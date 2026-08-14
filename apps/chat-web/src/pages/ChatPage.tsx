import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

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
import { asId } from "../chat/ids";
import { matchingSessions } from "../chat/published";
import { Composer } from "../chat/Composer";
import { SessionRail } from "../chat/SessionRail";
import { sessionTitle } from "../chat/sessionTitle";
import { Transcript } from "../chat/Transcript";
import { usePublishedAgents } from "../chat/usePublishedAgents";
import { useT } from "../i18n/locale";
import { RUN_ACTIONS } from "../runs/actions";
import { runQueryOptions, useRunEvents } from "../runs/useRunEvents";
import { isLiveStatus, statusLabel } from "../status";

export function ChatPage() {
  const t = useT();
  const auth = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const params = useParams();
  const workspaceId = asId(params.workspaceId);
  const agentId = asId(params.agentId);
  const routedSession = asId(params.sessionId);
  const listed = usePublishedAgents();
  const [runId, setRunId] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const enabled = workspaceId !== null && agentId !== null && auth.user !== null;

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
  const mine = matchingSessions(sessions.data ?? [], agentId ?? "", auth.user?.id ?? "");
  const activeSessionId = routedSession ?? mine[0]?.id ?? null;
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

  const openSession = useMutation({
    mutationFn: () =>
      api<SessionResponse>("/api/v1/sessions", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, session_mode: "persistent" }),
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions", workspaceId, agentId] });
      setRunId(null);
      setOptimistic(null);
      setError(null);
      navigate(`/${workspaceId}/${agentId}/${created.id}`);
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
        await queryClient.invalidateQueries({ queryKey: ["sessions", workspaceId, agentId] });
        navigate(`/${workspaceId}/${agentId}/${created.id}`);
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

  if (workspaceId === null || agentId === null) {
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
        agents={listed.rows}
        agentKey={`${workspaceId}:${agentId}`}
        sessions={railSessions}
        activeSessionId={activeSessionId}
        onAgent={(key) => {
          const [nextWorkspace, nextAgent] = key.split(":");
          if (nextWorkspace !== undefined && nextAgent !== undefined) {
            navigate(`/${nextWorkspace}/${nextAgent}`);
          }
        }}
        onSession={(id) => navigate(`/${workspaceId}/${agentId}/${id}`)}
        onNewChat={() => openSession.mutate()}
        creating={openSession.isPending}
      />
      <section className="chat-main">
        <header className="chat-head">
          <div>
            <h1>{agent.data.name}</h1>
            {run === undefined || run.finished_at !== null ? null : (
              <p className="chat-status">{statusLabel(run.status, t)}</p>
            )}
          </div>
          <div className="chat-actions">
            {(blocked ? headActions : (run?.available_actions ?? [])).map((action) => {
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
            agentName={agent.data.name}
            turns={messages.data ?? []}
            optimistic={optimistic}
            live={Boolean(live)}
            artifacts={artifacts.data ?? []}
            onDownload={(id, filename) => {
              void downloadArtifact(id, filename, workspaceId).catch((caught) =>
                setError(problemMessage(caught)),
              );
            }}
          />
        </div>
        <Composer disabled={false} sending={send.isPending} onSend={(text) => send.mutate(text)} />
      </section>
    </div>
  );
}
