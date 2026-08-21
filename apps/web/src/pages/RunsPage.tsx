import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Modal, Select, Space, Table, Tag, Typography } from "antd";
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, RunResponse, SessionResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { t } from "../i18n/zh-CN";
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

export function RunsPage() {
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
    // Only the submission dialog needs the agent list; asking for it on every
    // visit to the Run list would be a request nobody made.
    enabled: open && workspaceId !== null,
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
      render: (_: unknown, run: RunResponse) => (
        // The whole identifier: it is what the address bar, the API and the
        // logs all use, and half of one matches none of them.
        <Link to={`/workspaces/${workspaceId ?? ""}/runs/${run.id}`}>{run.id}</Link>
      ),
    },
    {
      title: t("runStatus"),
      key: "status",
      // The state machine's own name for the state. Translating it would put a
      // second vocabulary between the user and the events they are reading.
      //
      // The reason rides along with the status rather than getting a column
      // of its own: it is only ever present on a failure, and an empty
      // column on every healthy row would cost every reader something to
      // find the few rows that have anything in it. Untranslated for the
      // same reason as the status itself.
      render: (_: unknown, run: RunResponse) => (
        <Space direction="vertical" size={0}>
          <Tag>{run.status}</Tag>
          {run.failure_reason === null ? null : (
            <Typography.Text type="danger">{run.failure_reason}</Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: t("runQueue"),
      key: "queue",
      render: (_: unknown, run: RunResponse) => (
        <>
          <Typography.Text>{run.queue.status}</Typography.Text>
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

  return (
    <>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("runsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("runsIntro")}</Typography.Paragraph>
        </div>
        <Button type="primary" onClick={() => setOpen(true)}>
          {t("newRun")}
        </Button>
      </div>
      {/* Said out loud rather than papered over with a pager the platform
          cannot honour: the route takes no page or cursor, so any control here
          would sort a list that already arrived whole. */}
      <Alert className="page-alert" type="info" title={t("runsUnpaginated")} showIcon />
      <Card loading={runs.isPending} variant="borderless">
        <Table<RunResponse>
          rowKey="id"
          columns={columns}
          dataSource={runs.data ?? []}
          pagination={false}
          locale={{ emptyText: <Empty description={t("emptyRuns")} /> }}
        />
      </Card>
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
