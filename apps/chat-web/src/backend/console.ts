import { api, asApiError } from "../api/client";
import type {
  AgentResponse,
  ArtifactResponse,
  CanonicalMessage,
  RunResponse,
  SessionResponse,
} from "../api/types";
import type { ChatBackend, SignInInput, Viewer } from "./types";

/**
 * The management API's own user document.
 *
 * Local to this file on purpose: `is_platform_admin` is a control-console
 * fact, and letting it out of here is how a chat page ends up unable to serve
 * anyone who is not a workspace member.
 */
type UserResponse = {
  id: string;
  subject: string;
  display_name: string;
  status: string;
  is_platform_admin: boolean;
};

/**
 * The chat surface talking to the management API as a workspace member.
 *
 * Every `/api/v1` path, the `X-Workspace-Id` header and the browser session
 * cookie live in this file and nowhere else. That is the whole point: this is
 * what 0.1 has, and product design §7.1's 终端用户 Web Chat will be a second
 * file beside it rather than a second pass over the components.
 *
 * What makes this the *console's* backend and not an end user's:
 *
 * - it signs in with a subject and a password against `/auth/sessions`, which
 *   mints a platform-member session;
 * - it scopes every read to one workspace, a concept an end user does not
 *   have (§4.5);
 * - it lists Sessions for the whole workspace, not for the person, because
 *   the route it calls has no notion of "mine".
 *
 * A backend serving §4.5's end user would answer the same questions from
 * different routes, with a session that belongs to the person rather than to
 * the tenant.
 */
export function consoleBackend(workspaceId: string | null): ChatBackend {
  const workspace = workspaceId ?? "";
  const scope = workspaceId === null ? {} : { workspace };

  return {
    scopeKey: `console:${workspace}`,

    async viewer(): Promise<Viewer> {
      const user = await api<UserResponse>("/api/v1/auth/me");
      return { id: user.id, displayName: user.display_name, subject: user.subject };
    },

    async signIn(input: SignInInput): Promise<Viewer> {
      const user = await api<UserResponse>("/api/v1/auth/sessions", {
        method: "POST",
        body: JSON.stringify(input),
      });
      return { id: user.id, displayName: user.display_name, subject: user.subject };
    },

    async signOut(): Promise<void> {
      await api<void>("/api/v1/auth/sessions/current", { method: "DELETE" });
    },

    agent: (agentId) => api<AgentResponse>(`/api/v1/agents/${agentId}`, scope),

    listSessions: () => api<SessionResponse[]>("/api/v1/sessions", scope),

    createSession: (agentId) =>
      api<SessionResponse>("/api/v1/sessions", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, session_mode: "persistent" }),
      }),

    messages: (sessionId) =>
      api<CanonicalMessage[]>(`/api/v1/sessions/${sessionId}/messages`, scope),

    startRun: (sessionId, input) =>
      api<RunResponse>("/api/v1/runs", {
        ...scope,
        method: "POST",
        // A fresh key per send: the same key with a different body is a
        // conflict, and re-sending the same message is a new Run, not a
        // replay of the last one.
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ session_id: sessionId, input }),
      }),

    run: (runId) => api<RunResponse>(`/api/v1/runs/${runId}`, scope),

    act: (runId, action, expectedStateVersion) =>
      api<RunResponse>(`/api/v1/runs/${runId}/${action}`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ expected_state_version: expectedStateVersion }),
      }),

    artifacts: (runId) => api<ArtifactResponse[]>(`/api/v1/runs/${runId}/artifacts`, scope),

    async downloadArtifact(artifactId: string, filename: string): Promise<void> {
      // A bare link cannot carry the workspace header, so the bytes are
      // fetched and handed to an anchor as a blob.
      const response = await fetch(`/api/v1/artifacts/${artifactId}/content`, {
        credentials: "include",
        headers: { "X-Workspace-Id": workspace },
      });
      if (!response.ok) {
        throw await asApiError(response);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    },

    openEventStream(runId: string, cursor: number, signal: AbortSignal): Promise<Response> {
      const headers = new Headers();
      if (cursor > 0) {
        headers.set("Last-Event-ID", String(cursor));
      }
      // The workspace goes in the query string here, not the header: this one
      // route reads it there, and would answer `workspace_required` to the
      // header every other request uses.
      return fetch(
        `/api/v1/runs/${runId}/events?workspace_id=${encodeURIComponent(workspace)}`,
        { credentials: "include", headers, signal },
      );
    },
  };
}
