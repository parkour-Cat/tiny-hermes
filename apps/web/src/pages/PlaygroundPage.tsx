import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Input, Modal, Space, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

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
import { useT } from "../i18n/locale";
import { RUN_ACTIONS } from "../runs/actions";
import { mergeArtifacts, textOf, toolsOf, artifactIdsIn } from "../runs/transcript";
import { runQueryOptions, useRunEvents } from "../runs/useRunEvents";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

function matchingSessions(
  listed: SessionResponse[],
  agentId: string,
  userId: string,
): SessionResponse[] {
  return listed.filter(
    (session) =>
      session.agent_id === agentId &&
      session.session_mode === "persistent" &&
      session.caller_type === "user" &&
      session.caller_id === userId,
  );
}

export function PlaygroundPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const { agentId = "" } = useParams();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [modal, contextHolder] = Modal.useModal();
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const enabled = workspaceId !== null && agentId !== "" && auth.user !== null;

  const agent = useQuery({
    queryKey: ["agent", workspaceId, agentId] as const,
    queryFn: () => api<AgentResponse>(`/api/v1/agents/${agentId}`, scope),
    enabled,
  });
  const sessionQuery = ["playground-session", workspaceId, agentId, auth.user?.id] as const;
  const session = useQuery({
    queryKey: sessionQuery,
    queryFn: async () => {
      const listed = await api<SessionResponse[]>("/api/v1/sessions", scope);
      const mine = matchingSessions(listed, agentId, auth.user?.id ?? "");
      if (mine.length > 0) {
        return mine[mine.length - 1];
      }
      return api<SessionResponse>("/api/v1/sessions", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, session_mode: "persistent" }),
      });
    },
    enabled,
  });
  const sessionId = session.data?.id ?? null;
  const activeRunId = runId ?? session.data?.head_run_id ?? null;
  const messagesQuery = ["session-messages", workspaceId, sessionId] as const;
  const messages = useQuery({
    queryKey: messagesQuery,
    queryFn: () =>
      api<CanonicalMessage[]>(`/api/v1/sessions/${sessionId ?? ""}/messages`, scope),
    enabled: enabled && sessionId !== null,
  });
  const runOptions = runQueryOptions(workspaceId ?? "", activeRunId ?? "");
  const snapshot = useQuery({ ...runOptions, enabled: enabled && activeRunId !== null });
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
      void queryClient.invalidateQueries({ queryKey: messagesQuery });
      void queryClient.invalidateQueries({ queryKey: ["run-artifacts", workspaceId, activeRunId] });
    }
  }, [snapshot.data?.finished_at, queryClient, workspaceId, activeRunId, sessionId]);

  const openSession = useMutation({
    mutationFn: () =>
      api<SessionResponse>("/api/v1/sessions", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, session_mode: "persistent" }),
      }),
    onSuccess: (created) => {
      queryClient.setQueryData(sessionQuery, created);
      setRunId(null);
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  const send = useMutation({
    mutationFn: (text: string) =>
      api<RunResponse>("/api/v1/runs", {
        ...scope,
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ session_id: sessionId, input: text }),
      }),
    onSuccess: (created) => {
      setRunId(created.id);
      queryClient.setQueryData(runQueryOptions(workspaceId ?? "", created.id).queryKey, created);
      setInput("");
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
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
    const sendAction = () => control.mutateAsync({ target, action }).catch(() => undefined);
    if (offer.question === null) {
      void sendAction();
      return;
    }
    void modal.confirm({
      title: t(offer.label),
      content: t(offer.question),
      okText: t("confirm"),
      cancelText: t("cancel"),
      onOk: sendAction,
    });
  }

  const failed = [agent, session].find((query) => query.isError);
  if (failed !== undefined) {
    return (
      <Alert
        type="error"
        title={problemMessage(failed.error)}
        action={<Button onClick={() => void failed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }
  if (agent.data === undefined || session.data === undefined) {
    return <Card loading variant="borderless" />;
  }

  const run = snapshot.data;
  const blocked = run?.queue.status === "session_blocked";
  const headId = run?.queue.blocked_by_run_id ?? null;
  const headActions = blocked ? (run.queue.available_actions ?? []) : [];
  const turns = messages.data ?? [];
  const files = mergeArtifacts(
    artifacts.data ?? [],
        turns.flatMap((message) =>
          message.parts.flatMap((part) =>
            part.output === undefined ? [] : artifactIdsIn(part.output),
          ),
        ),
  );

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("playground")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("playgroundIntro")}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">
            <Link to={`/workspaces/${workspaceId}/agents/${agentId}`}>{agent.data.name}</Link>
            {" · "}
            <Typography.Text>{session.data.id}</Typography.Text>
          </Typography.Paragraph>
        </div>
        <Button loading={openSession.isPending} onClick={() => openSession.mutate()}>
          {t("newSession")}
        </Button>
      </div>
      {note === null ? null : <Alert className="page-alert" type="info" title={note} showIcon />}
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {events.error === null ? null : (
        <Alert className="page-alert" type="warning" title={events.error} showIcon />
      )}
      {blocked ? (
        <Alert
          className="page-alert"
          type="warning"
          title={t("sessionBlocked")}
          description={
            <Space wrap>
              {run.queue.head_status === undefined ? null : <Tag>{run.queue.head_status}</Tag>}
              {headActions.map((action) => {
                const offer = RUN_ACTIONS[action];
                return offer === undefined || headId === null ? null : (
                  <Button
                    key={`head-${action}`}
                    loading={control.isPending}
                    onClick={() => act(headId, action)}
                  >
                    {t(offer.label)}
                  </Button>
                );
              })}
            </Space>
          }
          showIcon
        />
      ) : null}
      {run === undefined ? null : (
        <Space wrap className="page-alert">
          <Tag>{run.status}</Tag>
          <Tag>{run.queue.status}</Tag>
          {run.available_actions.map((action) => {
            const offer = RUN_ACTIONS[action];
            return offer === undefined ? null : (
              <Button
                key={action}
                loading={control.isPending}
                onClick={() => act(run.id, action)}
              >
                {t(offer.label)}
              </Button>
            );
          })}
        </Space>
      )}
      <Card title={t("messagesSection")} variant="borderless" className="page-alert">
        {turns.length === 0 ? (
          <Empty description={t("emptyPlayground")} />
        ) : (
          turns.map((message, index) => (
            <article className="workspace-row" key={`${message.role}-${index}`}>
              <Tag>{message.role}</Tag>
              <Typography.Paragraph className="fact-note">{textOf(message)}</Typography.Paragraph>
            </article>
          ))
        )}
      </Card>
      <Card title={t("toolsCallsSection")} variant="borderless" className="page-alert">
        {toolsOf(turns).length === 0 ? (
          <Empty description={t("emptyTools")} />
        ) : (
          toolsOf(turns).map((round) => (
            <article className="workspace-row" key={round.callId || round.name}>
              <Typography.Text strong>{round.name}</Typography.Text>
              <Typography.Paragraph className="fact-note">
                {JSON.stringify(round.arguments)}
              </Typography.Paragraph>
              <Typography.Paragraph type="secondary">{round.output}</Typography.Paragraph>
            </article>
          ))
        )}
      </Card>
      <Card title={t("filesSection")} variant="borderless" className="page-alert">
        {files.length === 0 ? (
          <Empty description={t("emptyFiles")} />
        ) : (
          files.map((file) => (
            <Space key={file.id} className="workspace-row">
              <Typography.Text>{file.filename}</Typography.Text>
              <Button
                onClick={() =>
                  void downloadArtifact(file.id, file.filename, workspaceId ?? "").catch((caught) =>
                    setError(problemMessage(caught)),
                  )
                }
              >
                {t("downloadArtifact")}
              </Button>
            </Space>
          ))
        )}
      </Card>
      <Card variant="borderless">
        <Input.TextArea
          aria-label={t("composerPlaceholder")}
          rows={4}
          value={input}
          onChange={(event) => setInput(event.target.value)}
        />
        <Button
          type="primary"
          className="page-alert"
          disabled={input.trim() === "" || sessionId === null}
          loading={send.isPending}
          onClick={() => send.mutate(input.trim())}
        >
          {t("sendMessage")}
        </Button>
      </Card>
    </>
  );
}

