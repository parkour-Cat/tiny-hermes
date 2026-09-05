import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Tag, Typography } from "antd";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, MemoryResponse, SearchHitResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { EmptyState } from "../ui/EmptyState";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/**
 * What an Agent has asked to remember, and whether it may.
 *
 * §4.6 gives this to a workspace or platform administrator. The routes —
 * `pending`, `approve`, `reject` — shipped with M2's memory work and
 * neither console referenced any of them, so proposals accumulated in a
 * table nobody could read and no Agent's shared memory could ever grow.
 *
 * The body is shown **as proposed, word for word**, and there is no way to
 * edit it here. What gets written is this text, so this text is what has to
 * be read; a console that let a reviewer alter it first would be writing
 * its own memory under a review the proposal never received. That is the
 * same reason §16.3 has no "approve with changes".
 */
export function MemoryPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const scope = { workspace: workspaceId ?? "" };
  const pendingQuery = ["memories-pending", workspaceId] as const;

  const pending = useQuery({
    queryKey: pendingQuery,
    queryFn: () => api<MemoryResponse[]>("/api/v1/memories/pending", scope),
    enabled: workspaceId !== null,
  });
  const agents = useQuery({
    queryKey: ["agents", workspaceId] as const,
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", scope),
    enabled: workspaceId !== null,
  });

  const [query, setQuery] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const [writing, setWriting] = useState(false);
  const [form] = Form.useForm<{ agentId: string; body: string }>();

  // §4.6 gives searching somebody else's conversations to the same steward
  // that reviews memory, which is why it lives on this page rather than
  // beside Runs: one page, one permission, one thing to explain.
  const hits = useQuery({
    queryKey: ["session-search", workspaceId, asked] as const,
    queryFn: () =>
      api<SearchHitResponse[]>(
        `/api/v1/memories/search?q=${encodeURIComponent(asked ?? "")}`,
        scope,
      ),
    enabled: workspaceId !== null && asked !== null && asked !== "",
  });

  const writeShared = useMutation({
    mutationFn: (values: { agentId: string; body: string }) =>
      api<MemoryResponse>("/api/v1/memories/shared", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ agent_id: values.agentId, body: values.body }),
      }),
    onSuccess: () => {
      setWriting(false);
      form.resetFields();
      void queryClient.invalidateQueries({ queryKey: pendingQuery });
    },
    onError: (caught) =>
      form.setFields([{ name: "body", errors: [problemMessage(caught, t)] }]),
  });

  const decide = useMutation({
    mutationFn: (input: { id: string; decision: "approve" | "reject" }) =>
      api<MemoryResponse>(`/api/v1/memories/${input.id}/${input.decision}`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: pendingQuery }),
  });

  if (pending.isError) {
    // §4.6 gives review to an administrator, so a refusal is ordinary here.
    // An empty queue would tell a developer their Agents proposed nothing.
    return (
      <Alert
        type="warning"
        showIcon
        message={problemMessage(pending.error, t)}
        description={t("memoryForbiddenHint")}
      />
    );
  }

  const rows = pending.data ?? [];
  const named = new Map((agents.data ?? []).map((agent) => [agent.id, agent.name]));

  return (
    <>
      <div className="page-heading">
        <div>
          <Typography.Paragraph type="secondary">{t("memoryReviewIntro")}</Typography.Paragraph>
        </div>
        <Button type="primary" onClick={() => setWriting(true)}>
          {t("writeShared")}
        </Button>
      </div>

      <Card title={t("searchSessions")} variant="borderless" className="page-alert">
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary">{t("searchSessionsIntro")}</Typography.Paragraph>
          <Space wrap>
            <Input
              aria-label={t("searchSessions")}
              style={{ minWidth: 320 }}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onPressEnter={() => setAsked(query)}
            />
            <Button loading={hits.isFetching} onClick={() => setAsked(query)}>
              {t("searchRun")}
            </Button>
          </Space>
          {hits.isError ? (
            <Alert type="warning" showIcon message={problemMessage(hits.error, t)} />
          ) : null}
          {asked !== null && (hits.data ?? []).length === 0 && !hits.isFetching ? (
            <EmptyState title={t("searchNoHits")} />
          ) : null}
          {(hits.data ?? []).map((hit) => (
            <Card key={`${hit.session_id}-${hit.sequence}`} variant="borderless" className="page-alert">
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Space wrap>
                  <Tag>{hit.role}</Tag>
                  {hit.run_id === null ? null : (
                    <Link to={`/workspaces/${workspaceId}/runs/${hit.run_id}`}>{hit.run_id}</Link>
                  )}
                </Space>
                <Typography.Paragraph>{hit.snippet}</Typography.Paragraph>
                {hit.shortened ? (
                  // Said, not implied by an ellipsis: a reader who does not
                  // know they are holding part of a message reads it as the
                  // whole of one.
                  <Typography.Text type="secondary">{t("searchShortened")}</Typography.Text>
                ) : null}
              </Space>
            </Card>
          ))}
        </Space>
      </Card>

      <Card loading={pending.isPending} variant="borderless">
        {rows.length === 0 ? (
          <EmptyState title={t("memoryEmpty")} />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {rows.map((row) => (
              <Card key={row.id} variant="borderless" className="page-alert">
                <Space direction="vertical" size="small" style={{ width: "100%" }}>
                  <Space wrap>
                    <Tag>{row.kind}</Tag>
                    {/* Whose memory this becomes. An Agent's shared memory is
                        read by every Run of that Agent, so which Agent is
                        half of what is being decided. */}
                    <Typography.Text strong>{named.get(row.agent_id) ?? row.agent_id}</Typography.Text>
                    <Typography.Text type="secondary">{row.origin}</Typography.Text>
                    <Typography.Text type="secondary">{moment(row.created_at)}</Typography.Text>
                  </Space>
                  {/* The proposal itself, unedited and uneditable. */}
                  <pre className="skill-file-body">{row.body}</pre>
                  <Space wrap>
                    <Button
                      type="primary"
                      loading={decide.isPending}
                      onClick={() => decide.mutate({ id: row.id, decision: "approve" })}
                    >
                      {t("memoryApprove")}
                    </Button>
                    <Button
                      danger
                      loading={decide.isPending}
                      onClick={() => decide.mutate({ id: row.id, decision: "reject" })}
                    >
                      {t("memoryReject")}
                    </Button>
                  </Space>
                </Space>
              </Card>
            ))}
          </Space>
        )}
      </Card>
      <Modal
        open={writing}
        title={t("writeShared")}
        okText={t("saveName")}
        cancelText={t("cancel")}
        confirmLoading={writeShared.isPending}
        onCancel={() => setWriting(false)}
        onOk={() => void form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => writeShared.mutate(values)}
        >
          <Typography.Paragraph type="secondary">{t("writeSharedHint")}</Typography.Paragraph>
          <Form.Item name="agentId" label={t("memoryAgent")} rules={[{ required: true }]}>
            <Select
              options={(agents.data ?? []).map((agent) => ({
                value: agent.id,
                label: agent.name,
              }))}
            />
          </Form.Item>
          <Form.Item name="body" label={t("memoryBody")} rules={[{ required: true }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>
      {decide.isError ? (
        <Alert
          className="page-alert"
          type="warning"
          showIcon
          message={problemMessage(decide.error, t)}
        />
      ) : null}
    </>
  );
}
