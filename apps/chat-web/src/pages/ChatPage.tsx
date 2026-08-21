import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";

import { downloadArtifact } from "../api/artifacts";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  AgentResponse,
  ArtifactResponse,
  CanonicalMessage,
  RunResponse,
  SessionResponse,
} from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { AgentPicker } from "../chat/AgentPicker";
import { Composer } from "../chat/Composer";
import { downloadMarkdown, exportFilename, transcriptMarkdown } from "../chat/exportTranscript";
import { chatPath, matchSessionId, resolveChatRoute } from "../chat/paths";
import { matchingSessions } from "../chat/published";
import { loadSessionPrefs, saveSessionPrefs } from "../chat/sessionPrefs";
import { SessionRail } from "../chat/SessionRail";
import { isBlankSession, sessionTitle } from "../chat/sessionTitle";
import { Transcript } from "../chat/Transcript";
import { usePublishedAgents } from "../chat/usePublishedAgents";
import { useT } from "../i18n/locale";
import { RUN_ACTIONS } from "../runs/actions";
import { runQueryOptions, useRunEvents } from "../runs/useRunEvents";
import { isLiveStatus, statusLabel } from "../status";

const CHROME_ACTIONS = new Set(["pause", "resume", "cancel"]);

export function ChatPage() {
  const t = useT();
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const params = useParams();
  const listed = usePublishedAgents();
  const route = resolveChatRoute(params, listed.rows);
  const workspaceId = route.kind === "ok" ? route.workspaceId : null;
  const agentId = route.kind === "ok" ? route.agentId : null;
  const sessionRef = route.kind === "ok" ? route.sessionRef : null;
  const [runId, setRunId] = useState<string | null>(null);
  const [openedId, setOpenedId] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [prefs, setPrefs] = useState(loadSessionPrefs);
  const scope = { workspace: workspaceId ?? "" };
  const enabled = workspaceId !== null && agentId !== null && auth.user !== null;

  function go(sessionId?: string | null): void {
    const row = listed.rows.find(
      (item) => item.workspace.id === workspaceId && item.agent.id === agentId,
    );
    if (row !== undefined) {
      navigate(chatPath(row, listed.rows, sessionId));
      return;
    }
    if (workspaceId !== null && agentId !== null) {
      navigate(
        sessionId === undefined || sessionId === null
          ? `/${workspaceId}/${agentId}`
          : `/${workspaceId}/${agentId}/${sessionId}`,
      );
    }
  }

  const agent = useQuery({
    queryKey: ["agent", workspaceId, agentId] as const,
    queryFn: () => api<AgentResponse>(`/api/v1/agents/${agentId ?? ""}`, scope),
    enabled,
  });
  const sessions = useQuery({
    queryKey: ["sessions", workspaceId, agentId, auth.user?.id] as const,
    queryFn: () => api<SessionResponse[]>("/api/v1/sessions", scope),
    enabled,
  });
  const mine = matchingSessions(sessions.data ?? [], agentId ?? "", auth.user?.id ?? "").filter(
    (session) => !prefs.hidden.includes(session.id),
  );
  const routedSession =
    matchSessionId(
      [...mine.map((session) => session.id), openedId].filter((id): id is string => id !== null),
      sessionRef,
    ) ??
    (openedId !== null && (sessionRef === null || openedId.startsWith(sessionRef))
      ? openedId
      : null);
  const routedVisible =
    routedSession !== null && !prefs.hidden.includes(routedSession) ? routedSession : null;
  const activeSessionId = routedVisible ?? mine[0]?.id ?? null;
  const active = mine.find((session) => session.id === activeSessionId) ?? null;

  const titleQueries = useQueries({
    queries: mine.map((session) => ({
      queryKey: ["session-messages", workspaceId, session.id] as const,
      queryFn: () =>
        api<CanonicalMessage[]>(`/api/v1/sessions/${session.id}/messages`, scope),
    })),
  });
  const messages = useQuery({
    queryKey: ["session-messages", workspaceId, activeSessionId] as const,
    queryFn: () =>
      api<CanonicalMessage[]>(`/api/v1/sessions/${activeSessionId ?? ""}/messages`, scope),
    enabled: enabled && activeSessionId !== null,
  });

  const activeRunId = runId ?? active?.head_run_id ?? null;
  const snapshot = useQuery({
    ...runQueryOptions(workspaceId ?? "", activeRunId ?? ""),
    enabled: enabled && activeRunId !== null,
  });
  const events = useRunEvents({
    runId: enabled && activeRunId !== null ? activeRunId : null,
    workspaceId,
  });
  const artifacts = useQuery({
    queryKey: ["run-artifacts", workspaceId, activeRunId] as const,
    queryFn: () =>
      api<ArtifactResponse[]>(`/api/v1/runs/${activeRunId ?? ""}/artifacts`, scope),
    enabled: enabled && activeRunId !== null,
  });

  useEffect(() => {
    if (snapshot.data?.finished_at !== null && snapshot.data?.finished_at !== undefined) {
      void queryClient.invalidateQueries({
        queryKey: ["session-messages", workspaceId, activeSessionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["run-artifacts", workspaceId, activeRunId],
      });
    }
  }, [snapshot.data?.finished_at, queryClient, workspaceId, activeSessionId, activeRunId]);

  useEffect(() => {
    if (optimistic === null || messages.data === undefined) {
      return;
    }
    if (messages.data.some((message) => message.role === "user" && message.parts.some((part) => part.text === optimistic))) {
      setOptimistic(null);
    }
  }, [messages.data, optimistic]);

  useEffect(() => {
    if (workspaceId === null || agentId === null || listed.rows.length === 0) {
      return;
    }
    const row = listed.rows.find(
      (item) => item.workspace.id === workspaceId && item.agent.id === agentId,
    );
    if (row === undefined) {
      return;
    }
    const next = chatPath(row, listed.rows, activeSessionId);
    if (location.pathname !== next) {
      navigate(next, { replace: true });
    }
  }, [workspaceId, agentId, listed.rows, activeSessionId, location.pathname, navigate]);

  const openSession = useMutation({
    mutationFn: () =>
      api<SessionResponse>("/api/v1/sessions", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, session_mode: "persistent" }),
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions", workspaceId, agentId] });
      setOpenedId(created.id);
      setRunId(null);
      setOptimistic(null);
      setError(null);
      go(created.id);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  const send = useMutation({
    mutationFn: async (text: string) => {
      let sessionId = activeSessionId;
      if (sessionId === null) {
        const created = await api<SessionResponse>("/api/v1/sessions", {
          ...scope,
          method: "POST",
          body: JSON.stringify({ agent_id: agentId, session_mode: "persistent" }),
        });
        sessionId = created.id;
        setOpenedId(created.id);
        await queryClient.invalidateQueries({ queryKey: ["sessions", workspaceId, agentId] });
        go(created.id);
      }
      return api<RunResponse>("/api/v1/runs", {
        ...scope,
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ session_id: sessionId, input: text }),
      });
    },
    onMutate: (text) => setOptimistic(text),
    onSuccess: (created) => {
      setRunId(created.id);
      queryClient.setQueryData(runQueryOptions(workspaceId ?? "", created.id).queryKey, created);
      setError(null);
    },
    onError: (caught) => {
      setOptimistic(null);
      setError(problemMessage(caught));
    },
  });

  const control = useMutation({
    mutationFn: async ({ target, action }: { target: string; action: string }) => {
      const current = await api<RunResponse>(`/api/v1/runs/${target}`, scope);
      return api<RunResponse>(`/api/v1/runs/${target}/${action}`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ expected_state_version: current.state_version }),
      });
    },
    onSuccess: (updated, { action, target }) => {
      queryClient.setQueryData(runQueryOptions(workspaceId ?? "", target).queryKey, updated);
      if (target === activeRunId) {
        setRunId(updated.id);
      }
      const done = RUN_ACTIONS[action]?.done;
      setNote(done === undefined || done === null ? null : t(done));
      setError(null);
    },
    onError: (caught) => {
      setNote(null);
      setError(problemMessage(caught));
    },
  });

  function act(target: string, action: string): void {
    const offer = RUN_ACTIONS[action];
    if (offer === undefined) {
      return;
    }
    if (offer.question !== null && !window.confirm(t(offer.question))) {
      return;
    }
    void control.mutateAsync({ target, action }).catch(() => undefined);
  }

  if (route.kind === "pending") {
    return <p className="centered">{t("loading")}</p>;
  }
  if (route.kind === "invalid" || workspaceId === null || agentId === null) {
    return <p className="centered">{t("invalidAddress")}</p>;
  }

  const failed = [agent, sessions].find((query) => query.isError);
  if (failed !== undefined) {
    return (
      <p className="centered">
        {problemMessage(failed.error)}
        <button type="button" onClick={() => void failed.refetch()}>
          {t("retry")}
        </button>
      </p>
    );
  }
  if (agent.data === undefined || sessions.data === undefined) {
    return <p className="centered">{t("loading")}</p>;
  }
  if (agent.data.current_version_id === null) {
    return <p className="centered">{t("agentNotPublished")}</p>;
  }

  const run = snapshot.data;
  const blocked = run?.queue.status === "session_blocked";
  const headId = run?.queue.blocked_by_run_id ?? null;
  const headActions = blocked ? (run.queue.available_actions ?? []) : [];
  const live = isLiveStatus(run?.status) && run?.finished_at === null;
  const railSessions = mine.map((session, index) => ({
    id: session.id,
    title: sessionTitle(titleQueries[index]?.data ?? [], t("untitledChat")),
  }));

  return (
    <div className="chat-app">
      <SessionRail
        sessions={railSessions}
        activeSessionId={activeSessionId}
        prefs={prefs}
        onPrefs={(next) => {
          saveSessionPrefs(next);
          setPrefs(next);
        }}
        onSession={(id) => go(id)}
        onNewChat={() => {
          const unused = mine.find((session, index) => {
            if (prefs.archived.includes(session.id)) {
              return false;
            }
            return isBlankSession(session, titleQueries[index]?.data);
          });
          if (unused !== undefined) {
            setRunId(null);
            setOptimistic(null);
            setError(null);
            go(unused.id);
            return;
          }
          openSession.mutate();
        }}
        onHidden={(id) => {
          if (id === activeSessionId) {
            go(null);
          }
        }}
        creating={openSession.isPending}
      />
      <section className="chat-main">
        <header className="chat-head">
          <div className="chat-identity">
            <AgentPicker
              agents={listed.rows}
              agentKey={`${workspaceId}:${agentId}`}
              fallback={agent.data.name}
              onAgent={(key) => {
                const [nextWorkspace, nextAgent] = key.split(":");
                const next = listed.rows.find(
                  (item) => item.workspace.id === nextWorkspace && item.agent.id === nextAgent,
                );
                if (next !== undefined) {
                  navigate(chatPath(next, listed.rows));
                }
              }}
            />
            {run === undefined || run.finished_at !== null ? null : (
              <p className="chat-status">{statusLabel(run.status, t)}</p>
            )}
          </div>
          <div className="chat-actions">
            {(blocked ? headActions : live ? (run?.available_actions ?? []) : []).map((action) => {
              if (!CHROME_ACTIONS.has(action)) {
                return null;
              }
              const offer = RUN_ACTIONS[action];
              const target = blocked ? headId : run?.id;
              return offer === undefined || target === null || target === undefined ? null : (
                <button
                  type="button"
                  key={action}
                  disabled={control.isPending}
                  onClick={() => act(target, action)}
                >
                  {t(offer.label)}
                </button>
              );
            })}
          </div>
        </header>
        {note === null ? null : <p className="banner banner-info">{note}</p>}
        {error === null ? null : <p className="banner banner-warn">{error}</p>}
        {events.error === null ? null : <p className="banner banner-warn">{events.error}</p>}
        {blocked ? (
          <p className="banner banner-warn">
            {t("sessionBlocked")}
            {run.queue.position > 0
              ? ` ${t("queuePositionPrefix")}${run.queue.position}${t("queuePositionSuffix")}`
              : ""}
            {run.queue.head_status === undefined
              ? ""
              : ` · ${statusLabel(run.queue.head_status, t)}`}
            {` ${t("newChatHint")}`}
          </p>
        ) : null}
        <div className="chat-scroll">
          <Transcript
            turns={messages.data ?? []}
            optimistic={optimistic}
            live={Boolean(live)}
            artifacts={artifacts.data ?? []}
            canRetry={!live && (run?.available_actions ?? []).includes("retry")}
            onDownload={(id, filename) => {
              void downloadArtifact(id, filename, workspaceId).catch((caught) =>
                setError(problemMessage(caught)),
              );
            }}
            onRetry={() => {
              if (run !== undefined) {
                void control.mutateAsync({ target: run.id, action: "retry" }).catch(() => undefined);
              }
            }}
          />
        </div>
        <Composer
          disabled={false}
          sending={send.isPending}
          live={Boolean(live)}
          canExport={(messages.data ?? []).length > 0}
          onSend={(text) => send.mutate(text)}
          onExport={() => {
            const turns = messages.data ?? [];
            if (turns.length === 0) {
              return;
            }
            downloadMarkdown(
              exportFilename(agent.data.alias, activeSessionId),
              transcriptMarkdown(agent.data.name, turns, {
                user: t("userRole"),
                agent: t("agentRole"),
              }),
            );
          }}
          onStop={() => {
            if (run === undefined) {
              return;
            }
            const action = (run.available_actions ?? []).includes("cancel")
              ? "cancel"
              : (run.available_actions ?? []).includes("pause")
                ? "pause"
                : null;
            if (action !== null) {
              void control.mutateAsync({ target: run.id, action }).catch(() => undefined);
            }
          }}
        />
      </section>
    </div>
  );
}
