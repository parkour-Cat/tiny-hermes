import { useMutation, useQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { CanonicalMessage, EndUserRunResponse, EndUserSessionResponse } from "../api/types";
import { ApprovalBanner } from "../chat/ApprovalBanner";
import { Composer } from "../chat/Composer";
import { downloadMarkdown, exportFilename, transcriptMarkdown } from "../chat/exportTranscript";
import { chatPath, isAgentAlias, matchSessionId } from "../chat/paths";
import { forgetSessionId, loadKnownSessionIds, rememberSessionId } from "../chat/localSessions";
import { loadSessionPrefs, saveSessionPrefs } from "../chat/sessionPrefs";
import { SessionRail } from "../chat/SessionRail";
import { sessionTitle } from "../chat/sessionTitle";
import { Transcript } from "../chat/Transcript";
import { useT } from "../i18n/locale";
import { cancelEndUserRun, useEndUserRun } from "../runs/useEndUserRun";
import { useEndUserApprovals } from "../runs/useEndUserApprovals";
import { isLiveStatus, statusLabel } from "../status";

/**
 * The end-user chat surface. `runs/presentation/routes.py`'s `Console`
 * cousin drives the same three visible pieces (rail, transcript, composer)
 * off `/api/v1/{sessions,runs}` and a workspace a signed-in member chose
 * from a list. This one drives them off `/api/v1/end-user/*`, an Agent
 * alias the host page named at embed time, and a Session list this device
 * remembers rather than one the platform lists (`localSessions.ts`'s own
 * docstring says why there is no such list to ask for).
 *
 * No pause/resume/retry and no artifact download: none of those have an
 * end-user route, and plan §10 built cancel but deliberately not the other
 * two — this chat surface has no place in its UI for "pause" (there is
 * nothing here shaped like the console's pause button), and cancel is the
 * one action both erasure and "I don't want this any more" need. The
 * composer's existing "stop" affordance is what §10 wires cancel through
 * (below), asking for confirmation first because unlike a token stream this
 * one cannot be un-stopped. A Run that stopped for its own `user_confirmation`
 * is answered through `ApprovalBanner`, §10's other door — before this task
 * a Run in that state had a producer and no way for its own end user to
 * reach it at all (see `useEndUserApprovals`'s own docstring). The composer
 * still queues a second message onto a busy Session exactly the way the
 * console's does (`queue.status === "session_blocked"` is the same field
 * either surface reads), because queuing was never a console-only
 * capability to begin with.
 */
export function ChatPage() {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const params = useParams();
  const alias = isAgentAlias(params.alias) ? params.alias : null;
  const sessionRef = params.sessionRef ?? null;

  const [runId, setRunId] = useState<string | null>(null);
  const [openedId, setOpenedId] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [prefs, setPrefs] = useState(loadSessionPrefs);
  const [confirmingCancel, setConfirmingCancel] = useState(false);

  const known = alias === null ? [] : loadKnownSessionIds(alias);
  const routedSession =
    matchSessionId([...known, openedId].filter((id): id is string => id !== null), sessionRef) ??
    (openedId !== null && (sessionRef === null || openedId.startsWith(sessionRef))
      ? openedId
      : null);
  const activeSessionId =
    routedSession !== null && !prefs.hidden.includes(routedSession) ? routedSession : null;

  function go(sessionId?: string | null): void {
    if (alias === null) {
      return;
    }
    navigate(chatPath(alias, sessionId));
  }

  const titleQueries = useQueries({
    queries: known
      .filter((id) => !prefs.hidden.includes(id))
      .map((id) => ({
        queryKey: ["end-user-messages", id] as const,
        queryFn: () => api<CanonicalMessage[]>(`/api/v1/end-user/sessions/${id}/messages`),
      })),
  });
  const messages = useQuery({
    queryKey: ["end-user-messages", activeSessionId] as const,
    queryFn: () =>
      api<CanonicalMessage[]>(`/api/v1/end-user/sessions/${activeSessionId ?? ""}/messages`),
    enabled: activeSessionId !== null,
  });

  const activeRunId = runId ?? null;
  const snapshot = useEndUserRun(activeRunId);
  const approvals = useEndUserApprovals(snapshot.data?.status === "waiting_approval");
  const activeApproval =
    approvals.data?.find((item) => item.run_id === activeRunId) ?? null;

  useEffect(() => {
    if (snapshot.data?.finished_at !== null && snapshot.data?.finished_at !== undefined) {
      void queryClient.invalidateQueries({
        queryKey: ["end-user-messages", activeSessionId],
      });
    }
  }, [snapshot.data?.finished_at, queryClient, activeSessionId]);

  useEffect(() => {
    if (optimistic === null || messages.data === undefined) {
      return;
    }
    if (
      messages.data.some(
        (message) => message.role === "user" && message.parts.some((part) => part.text === optimistic),
      )
    ) {
      setOptimistic(null);
    }
  }, [messages.data, optimistic]);

  useEffect(() => {
    if (alias === null || activeSessionId === null) {
      return;
    }
    const next = chatPath(alias, activeSessionId);
    if (location.pathname !== next) {
      navigate(next, { replace: true });
    }
  }, [alias, activeSessionId, location.pathname, navigate]);

  const send = useMutation({
    mutationFn: async (text: string) => {
      if (alias === null) {
        throw new Error(t("invalidAddress"));
      }
      let sessionId = activeSessionId;
      if (sessionId === null) {
        const created = await api<EndUserSessionResponse>(`/api/v1/end-user/agents/${alias}/sessions`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        sessionId = created.id;
        rememberSessionId(alias, created.id);
        setOpenedId(created.id);
        go(created.id);
      }
      return api<EndUserRunResponse>(`/api/v1/end-user/sessions/${sessionId}/runs`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ input: text }),
      });
    },
    onMutate: (text) => setOptimistic(text),
    onSuccess: (created) => {
      setRunId(created.id);
      queryClient.setQueryData(["end-user-run", created.id], created);
      setError(null);
    },
    onError: (caught) => {
      setOptimistic(null);
      setError(problemMessage(caught, t));
    },
  });

  const cancel = useMutation({
    mutationFn: (input: { runId: string; stateVersion: number }) =>
      cancelEndUserRun(input.runId, input.stateVersion),
    onSuccess: (cancelled) => {
      setConfirmingCancel(false);
      setError(null);
      queryClient.setQueryData(["end-user-run", cancelled.id], cancelled);
    },
    onError: (caught) => {
      setConfirmingCancel(false);
      setError(problemMessage(caught, t));
    },
  });

  useEffect(() => {
    // A confirmation left open past the moment its Run stopped being
    // cancellable — finished on its own, or was cancelled through a second
    // tab — is a button that would now only produce a stale-version error.
    if (snapshot.data?.finished_at != null) {
      setConfirmingCancel(false);
    }
  }, [snapshot.data?.finished_at]);

  if (alias === null) {
    return <p className="centered">{t("invalidAddress")}</p>;
  }

  const run = snapshot.data;
  const blocked = run?.queue.status === "session_blocked";
  const live = isLiveStatus(run?.status) && run?.finished_at === null;
  const railSessions = known
    .filter((id) => !prefs.hidden.includes(id))
    .map((id, index) => ({
      id,
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
          // A Session whose messages have loaded and come back empty is
          // free to reuse, the same "don't mint a new one just to leave the
          // last empty one orphaned" rule the console follows
          // (`isBlankSession`) — restated here on messages alone, since
          // `EndUserSessionResponse` carries no `head_run_id` to read for a
          // Session this device only knows the id of (task-9 review
          // finding F narrowed it down to just `id`).
          const unused = known.find(
            (id, index) =>
              !prefs.archived.includes(id) &&
              titleQueries[index]?.data !== undefined &&
              sessionTitle(titleQueries[index]?.data ?? [], "") === "",
          );
          setRunId(null);
          setOptimistic(null);
          setError(null);
          go(unused ?? null);
        }}
        onHidden={(id) => {
          forgetSessionId(alias, id);
          if (id === activeSessionId) {
            go(null);
          }
        }}
        creating={false}
      />
      <section className="chat-main">
        <header className="chat-head">
          <div className="chat-identity">
            <h1>{alias}</h1>
            {run === undefined || run.finished_at !== null ? null : (
              <p className="chat-status">{statusLabel(run.status, t)}</p>
            )}
          </div>
        </header>
        {error === null ? null : <p className="banner banner-warn">{error}</p>}
        {blocked ? (
          <p className="banner banner-warn">
            {t("sessionBlocked")}
            {run.queue.position > 0
              ? ` ${t("queuePositionPrefix")}${run.queue.position}${t("queuePositionSuffix")}`
              : ""}
            {` ${t("newChatHint")}`}
          </p>
        ) : null}
        {activeApproval === null ? null : <ApprovalBanner approval={activeApproval} />}
        {confirmingCancel && run !== undefined ? (
          <div className="banner banner-warn">
            <p>{t("cancelRunConfirm")}</p>
            <div className="approval-actions">
              <button
                type="button"
                className="is-danger"
                disabled={cancel.isPending}
                onClick={() =>
                  cancel.mutate({ runId: run.id, stateVersion: run.state_version })
                }
              >
                {t("cancelRunButton")}
              </button>
              <button
                type="button"
                disabled={cancel.isPending}
                onClick={() => setConfirmingCancel(false)}
              >
                {t("cancel")}
              </button>
            </div>
          </div>
        ) : null}
        <div className="chat-scroll">
          <Transcript
            turns={messages.data ?? []}
            optimistic={optimistic}
            live={Boolean(live)}
            artifacts={[]}
            canRetry={false}
            onDownload={() => setError(t("artifactUnavailable"))}
            onRetry={() => undefined}
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
              exportFilename(alias, activeSessionId),
              transcriptMarkdown(alias, turns, {
                user: t("userRole"),
                agent: t("agentRole"),
              }),
            );
          }}
          onStop={() => setConfirmingCancel(true)}
        />
      </section>
    </div>
  );
}
