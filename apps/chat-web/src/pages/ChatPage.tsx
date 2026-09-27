import { useMutation, useQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";

import { api } from "../api/client";
import { currentEndUserIdentity, type EndUserIdentity } from "../api/session";
import { QueryProvider } from "../api/QueryProvider";
import { problemMessage } from "../api/messages";
import type { CanonicalMessage, EndUserRunResponse, EndUserSessionResponse } from "../api/types";
import { AgentPicker } from "../chat/AgentPicker";
import { ApprovalBanner } from "../chat/ApprovalBanner";
import { Composer } from "../chat/Composer";
import { clearDraft, loadPendingSend, moveDraft, savePendingSend } from "../chat/drafts";
import { downloadMarkdown, exportFilename, transcriptMarkdown } from "../chat/exportTranscript";
import { chatPath, isAgentAlias, matchSessionId } from "../chat/paths";
import { forgetSessionId, loadKnownSessions, rememberSessionId } from "../chat/localSessions";
import { loadSessionPrefs, saveSessionPrefs } from "../chat/sessionPrefs";
import { SessionRail } from "../chat/SessionRail";
import { sessionTitle } from "../chat/sessionTitle";
import { Transcript } from "../chat/Transcript";
import { SavedFiles } from "../chat/SavedFiles";
import { useEndUserAgents } from "../chat/useEndUserAgents";
import { useT } from "../i18n/locale";
import { cancelEndUserRun, steerEndUserRun, useEndUserRun } from "../runs/useEndUserRun";
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
  const identity = useQuery({ queryKey: ["end-user-identity"], queryFn: currentEndUserIdentity, retry: false });
  if (identity.isError) return <main className="auth">
    <p role="alert">{problemMessage(identity.error, t)}</p>
    <button onClick={() => { void identity.refetch(); }}>{t("retry")}</button>
  </main>;
  if (identity.data === undefined || !identity.isFetchedAfterMount) return <p className="centered">{t("loading")}</p>;
  return <>
    {identity.isFetching ? <p className="centered">{t("loading")}</p> : null}
    <div hidden={identity.isFetching}>
      <QueryProvider key={`${identity.data.workspace_id}:${identity.data.end_user_id}`}>
        <ChatConversation identity={identity.data} onIdentityChanged={async () => { await identity.refetch(); }} />
      </QueryProvider>
    </div>
  </>;
}

function ChatConversation({ identity, onIdentityChanged }: {
  identity: EndUserIdentity;
  onIdentityChanged: () => Promise<void>;
}) {
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
  const pendingSend = useRef<{ sessionId: string; text: string; key: string } | null>(null);
  const [composerEpoch, setComposerEpoch] = useState(0);

  const knownSessions = alias === null ? [] : loadKnownSessions(alias);
  const known = knownSessions.map((session) => session.id);
  const agents = useEndUserAgents();
  const routedSession =
    matchSessionId([...known, openedId].filter((id): id is string => id !== null), sessionRef) ??
    (openedId !== null && (sessionRef === null || openedId.startsWith(sessionRef))
      ? openedId
      : null);
  const activeSessionId =
    routedSession !== null && !prefs.hidden.includes(routedSession) ? routedSession : null;
  const draftKey = JSON.stringify([identity.workspace_id, identity.end_user_id, alias, activeSessionId]);
  const currentDraftKey = useRef(draftKey);
  useEffect(() => { currentDraftKey.current = draftKey; }, [draftKey]);

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
    mutationFn: async ({ text, source }: { text: string; source: string }) => {
      const current = await currentEndUserIdentity();
      if (current.end_user_id !== identity.end_user_id || current.workspace_id !== identity.workspace_id) {
        await onIdentityChanged();
        throw new Error(t("draftIdentityChanged"));
      }
      if (alias === null) {
        throw new Error(t("invalidAddress"));
      }
      const stored = loadPendingSend(source);
      const previous = pendingSend.current?.sessionId === (activeSessionId ?? "") ? pendingSend.current : stored;
      const attempt = { text, key: previous?.text === text ? previous.key : crypto.randomUUID() };
      if (!savePendingSend(source, attempt)) throw new Error(t("draftRetryStorageFailed"));
      let sessionId = activeSessionId;
      if (sessionId === null) {
        const created = await api<EndUserSessionResponse>(`/api/v1/end-user/agents/${alias}/sessions`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        sessionId = created.id;
        if (!moveDraft(source, JSON.stringify([identity.workspace_id, identity.end_user_id, alias, sessionId]))) {
          throw new Error(t("draftRetryStorageFailed"));
        }
        rememberSessionId(alias, created.id);
        setOpenedId(created.id);
        go(created.id);
      }
      // A lost response may hide an accepted Run; retry the same request identity.
      const target = JSON.stringify([identity.workspace_id, identity.end_user_id, alias, sessionId]);
      pendingSend.current = { sessionId, ...attempt };
      const run = await api<EndUserRunResponse>(`/api/v1/end-user/sessions/${sessionId}/runs`, {
        method: "POST",
        headers: { "Idempotency-Key": attempt.key },
        body: JSON.stringify({ input: text }),
      });
      return { run, target };
    },
    onMutate: ({ text }) => setOptimistic(text),
    onSuccess: ({ run: created, target }, { source }) => {
      clearDraft(source);
      clearDraft(target);
      pendingSend.current = null;
      if (currentDraftKey.current !== source && currentDraftKey.current !== target) return;
      if (source !== target) setComposerEpoch((epoch) => epoch + 1);
      setRunId(created.id);
      setSteered(false);
      queryClient.setQueryData(["end-user-run", created.id], created);
      setError(null);
    },
    onError: (caught) => {
      setOptimistic(null);
      setError(problemMessage(caught, t));
    },
  });

  const [steered, setSteered] = useState(false);
  const steer = useMutation({
    mutationFn: (input: { runId: string; text: string }) => steerEndUserRun(input.runId, input.text),
    onSuccess: () => {
      setSteered(true);
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
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
      createdAt: knownSessions.find((session) => session.id === id)?.createdAt ?? "",
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
          setOpenedId(null);
          setOptimistic(null);
          setError(null);
          go(unused ?? null);
        }}
        onHidden={(id) => {
          clearDraft(JSON.stringify([identity.workspace_id, identity.end_user_id, alias, id]));
          forgetSessionId(alias, id);
          if (id === activeSessionId) {
            setOpenedId(null);
            go(null);
          }
        }}
        creating={false}
      />
      <section className="chat-main">
        <header className="chat-head">
          <div className="chat-identity">
            {/* The name, and a way to the other Agents the credential names.
                The alias stays in the address, where a bookmark wants it. */}
            <AgentPicker
              agents={agents.data ?? []}
              alias={alias}
              onAgent={(next) => {
                setRunId(null);
                setOpenedId(null);
                setOptimistic(null);
                setError(null);
                navigate(chatPath(next));
              }}
            />
            {run === undefined || run.finished_at !== null ? null : (
              <p className="chat-status">{statusLabel(run.status, t)}</p>
            )}
          </div>
        </header>
        {error === null ? null : <p className="banner banner-warn">{error}</p>}
        {steered && live ? (
          <p className="banner" role="status">
            {t("steerAccepted")}
          </p>
        ) : null}
        {run !== undefined && run.finished_at !== null && (run.undelivered_steers ?? []).length > 0 ? (
          <div className="banner banner-warn">
            <p>{t("steersUndelivered")}</p>
            <ul>
              {(run.undelivered_steers ?? []).map((said, index) => (
                <li key={`${String(index)}-${said}`}>
                  <span>{said}</span>{" "}
                  <button
                    type="button"
                    disabled={send.isPending}
                    onClick={() => send.mutate({ text: said, source: draftKey })}
                  >
                    {t("steerResend")}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
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
          <SavedFiles key={activeSessionId} sessionId={activeSessionId} refreshToken={snapshot.data?.state_version} />
        </div>
        <Composer
          key={`${draftKey}:${composerEpoch}`}
          draftKey={draftKey}
          onDraftReset={() => { pendingSend.current = null; }}
          disabled={false}
          sending={send.isPending}
          live={Boolean(live)}
          canExport={(messages.data ?? []).length > 0}
          onSend={async (text) => { await send.mutateAsync({ text, source: draftKey }); }}
          onSteer={
            run === undefined
              ? undefined
              : async (text) => { await steer.mutateAsync({ runId: run.id, text }); }
          }
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
                withdrawn: t("withdrawnTurn"),
              }),
            );
          }}
          onStop={() => setConfirmingCancel(true)}
        />
      </section>
    </div>
  );
}
