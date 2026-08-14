import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Modal, Select, Table, Typography } from "antd";
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, RunResponse, SessionResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { PageHeading } from "../layout/ConsoleChrome";
import { statusLabel } from "../status";
import { EmptyState } from "../ui/EmptyState";
import { StatusTag } from "../ui/StatusTag";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type SubmitValues = {
  agent_id: string;
  input: string;
};

/**
 * One submission, and everything that must not change while it is retried.
 *
 * The key identifies the request, so pressing 提交 again after a failure has to
 * send the same key with the same body — a new key would submit a second Run,
 * and the same key with a different body is what the server refuses as
 * `idempotency_key_reused`. That includes the Session: it is created first and
 * then named in the Run's body, so a retry reuses the one already opened
 * instead of leaving an empty Session behind on every attempt.
 */
type Attempt = {
  signature: string;
  key: string;
  sessionId: string | null;
};

function runCaption(
  run: RunResponse,
  agents: AgentResponse[] | undefined,
  t: (key: "runUnknownAgent" | "runTurnPrefix" | "runTurnSuffix") => string,
): { name: string; turn: string } {
  const agent = (agents ?? []).find((entry) => entry.current_version_id === run.agent_version_id);
  return {
    name: agent?.name ?? t("runUnknownAgent"),
    turn: `${t("runTurnPrefix")}${String(run.session_sequence)}${t("runTurnSuffix")}`,
  };
}

export function RunsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const navigate = useNavigate();
  const [form] = Form.useForm<SubmitValues>();
  const [open, setOpen] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const attempt = useRef<Attempt | null>(null);
  const scope = { workspace: workspaceId ?? "" };

  const runs = useQuery({
    queryKey: ["runs", workspaceId] as const,
    queryFn: () => api<RunResponse[]>("/api/v1/runs", scope),
    enabled: workspaceId !== null,
  });
  const agents = useQuery({
    queryKey: ["agents", workspaceId] as const,
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", scope),
    enabled: workspaceId !== null,
  });

  const submit = useMutation({
    mutationFn: async (values: SubmitValues) => {
      const signature = JSON.stringify(values);
      if (attempt.current === null || attempt.current.signature !== signature) {
        attempt.current = { signature, key: crypto.randomUUID(), sessionId: null };
      }
      const pending = attempt.current;
      if (pending.sessionId === null) {
        // Sessions as a first-class concept are phase four. What phase two owes
        // the user is a correct submission, which needs exactly one Session.
        const session = await api<SessionResponse>("/api/v1/sessions", {
          ...scope,
          method: "POST",
          body: JSON.stringify({ agent_id: values.agent_id, session_mode: "persistent" }),
        });
        pending.sessionId = session.id;
      }
      return api<RunResponse>("/api/v1/runs", {
        ...scope,
        method: "POST",
        headers: { "Idempotency-Key": pending.key },
        body: JSON.stringify({ session_id: pending.sessionId, input: values.input }),
      });
    },
    onSuccess: (run) => {
      attempt.current = null;
      setOpen(false);
      setSubmitError(null);
      form.resetFields();
      navigate(`/workspaces/${workspaceId ?? ""}/runs/${run.id}`);
    },
    onError: (caught) => {
      // Nothing is sent again here. A console that resubmits on its own turns
      // the idempotency record into decoration, and the one refusal that says
      // the key is spent is the one where sending it again cannot succeed.
      if (problemMessage(caught) === t("idempotencyKeyReused")) {
        attempt.current = null;
      }
      setSubmitError(problemMessage(caught));
    },
  });

  if (runs.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(runs.error)}
        action={<Button onClick={() => void runs.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  const columns = [
    {
      title: t("runColumn"),
      key: "id",
      render: (_: unknown, run: RunResponse) => {
        const caption = runCaption(run, agents.data, t);
        return (
          <Link
            className="th-run-link"
            to={`/workspaces/${workspaceId ?? ""}/runs/${run.id}`}
            aria-label={`${caption.name} · ${caption.turn}`}
          >
            <span className="th-run-name">{caption.name}</span>
            <span className="th-run-meta">{caption.turn}</span>
          </Link>
        );
      },
    },
    {
      title: t("runStatus"),
      key: "status",
      render: (_: unknown, run: RunResponse) => <StatusTag code={run.status} />,
    },
    {
      title: t("runQueue"),
      key: "queue",
      render: (_: unknown, run: RunResponse) => (
        <>
          <Typography.Text>{statusLabel(run.queue.status, t)}</Typography.Text>
          {run.queue.status === "head" ? null : (
            <Typography.Paragraph type="secondary" className="fact-note">
              {`${t("queuePositionPrefix")}${run.queue.position}${t("queuePositionSuffix")}`}
            </Typography.Paragraph>
          )}
        </>
      ),
    },
    {
      title: t("runSessionSequence"),
      key: "session_sequence",
      render: (_: unknown, run: RunResponse) => run.session_sequence,
    },
    {
      title: t("runCreatedAt"),
      key: "created_at",
      render: (_: unknown, run: RunResponse) => moment(run.created_at),
    },
    {
      title: t("runFinishedAt"),
      key: "finished_at",
      render: (_: unknown, run: RunResponse) =>
        run.finished_at === null ? t("notFinished") : moment(run.finished_at),
    },
  ];

  const rows = runs.data ?? [];

  return (
    <>
      <PageHeading
        kicker={t("workspaceTitle")}
        title={t("runsTitle")}
        intro={t("runsIntro")}
        extra={
          <Button type="primary" onClick={() => setOpen(true)}>
            {t("newRun")}
          </Button>
        }
      />
      {runs.isPending ? (
        <Card loading variant="borderless" />
      ) : rows.length === 0 ? (
        <Card variant="borderless">
          <EmptyState title={t("emptyRuns")} />
        </Card>
      ) : (
        <Card variant="borderless">
          <Table<RunResponse>
            rowKey="id"
            columns={columns}
            dataSource={rows}
            pagination={false}
          />
        </Card>
      )}
      <Modal
        open={open}
        title={t("newRun")}
        okText={t("submit")}
        cancelText={t("cancel")}
        confirmLoading={submit.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => void form.submit()}
      >
        {submitError === null ? null : (
          <Alert className="page-alert" type="error" title={submitError} showIcon />
        )}
        <Form<SubmitValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => submit.mutate(values)}
        >
          <Form.Item
            name="agent_id"
            label={t("runAgent")}
            rules={[{ required: true, message: t("required") }]}
          >
            <Select
              loading={agents.isPending}
              options={(agents.data ?? []).map((agent) => ({
                value: agent.id,
                label: agent.name,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="input"
            label={t("runInput")}
            rules={[
              { required: true, whitespace: true, message: t("required") },
              { max: 32_768, message: t("runInputMaximum") },
            ]}
          >
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
