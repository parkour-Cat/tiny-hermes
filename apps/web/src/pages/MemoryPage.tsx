import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Alert, Button, Card, Space, Tag, Typography, Radio, Modal, Input } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, MemoryResponse } from "../api/types";
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
 * edit a pending proposal here. What gets approved is this text, so this text must
 * be read; a console that let a reviewer alter it first would be writing
 * its own memory under a review the proposal never received. That is the
 * same reason §16.3 has no "approve with changes".
 */
export function MemoryPage({ agentId, focusId }: { agentId?: string; focusId?: string }) {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const scope = { workspace: workspaceId ?? "" };
  const [view, setView] = useState(agentId ? "active" : "pending");
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<MemoryResponse | null>(null);
  const [body, setBody] = useState("");
  const [modal, contextHolder] = Modal.useModal();
  const pendingQuery = ["memories-pending", workspaceId, view, agentId, offset, focusId] as const;

  const pending = useQuery({
    queryKey: pendingQuery,
    queryFn: async () => {
      if (focusId) return { items: [await api<MemoryResponse>(`/api/v1/memories/${focusId}`, scope)], has_more: false };
      if (view === "pending" && !agentId) return { items: await api<MemoryResponse[]>("/api/v1/memories/pending", scope), has_more: false };
      const query = new URLSearchParams({ status: view, limit: "20", offset: String(offset) });
      if (agentId) { query.set("agent_id", agentId); query.set("kind", "shared"); }
      return api<{ items: MemoryResponse[]; has_more: boolean }>(`/api/v1/memories?${query}`, scope);
    },
    enabled: workspaceId !== null,
  });
  const agents = useQuery({
    queryKey: ["agents", workspaceId] as const,
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", scope),
    enabled: workspaceId !== null,
  });

  const decide = useMutation({
    mutationFn: (input: { id: string; decision: "approve" | "reject" }) =>
      api<MemoryResponse>(`/api/v1/memories/${input.id}/${input.decision}`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memories-pending", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["inbox-count", workspaceId] });
    },
  });

  const change = useMutation({
    mutationFn: ({ row, retire }: { row: MemoryResponse; retire: boolean }) => api<MemoryResponse>(`/api/v1/memories/shared/${row.id}`, {
      ...scope, method: "PATCH", body: JSON.stringify({ expected_updated_at: row.updated_at, ...(retire ? { status: "rejected" } : { body }) }),
    }),
    onSuccess: () => { setEditing(null); void queryClient.invalidateQueries({ queryKey: ["memories-pending", workspaceId] }); },
  });

  if (pending.isError) {
    // §4.6 gives review to an administrator, so a refusal is ordinary here.
    // An empty queue would tell a developer their Agents proposed nothing.
    return (
      <Alert
        type="warning"
        showIcon
        message={problemMessage(pending.error, t)}
        action={<Button onClick={() => void pending.refetch()}>{t("retry")}</Button>}
      />
    );
  }

  const rows = (pending.data?.items ?? []).filter((row) => !focusId || row.id === focusId);
  const named = new Map((agents.data ?? []).map((agent) => [agent.id, agent.name]));

  return (
    <>
      {contextHolder}
      {!focusId && <Radio.Group className="page-alert" value={view} onChange={(event) => { setView(String(event.target.value)); setOffset(0); }} options={[
        ...(!agentId ? [{ value: "pending", label: t("memoryPending") }] : []),
        { value: "active", label: t("memoryActive") }, { value: "rejected", label: t("memoryRetired") },
      ]} />}
      <Space wrap className="page-alert">
        <Link to={`/workspaces/${workspaceId}/records#sessions`}>{t("searchSessions")}</Link>
        <Link to={`/workspaces/${workspaceId}/agents`}>{t("memoryManageAtAgent")}</Link>
      </Space>
      <Card loading={pending.isPending} variant="borderless">
        {rows.length === 0 ? (
          <EmptyState title={t("memoryEmpty")} />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {rows.map((row) => (
              <Card key={row.id} variant="borderless" className="page-alert memory-review">
                <Space direction="vertical" size="small" style={{ width: "100%" }}>
                  <Space wrap>
                    <Tag>{row.kind}</Tag>
                    <Tag>{t(row.status === "pending" ? "memoryPending" : row.status === "active" ? "memoryActive" : "memoryRetired")}</Tag>
                    {/* Whose memory this becomes. An Agent's shared memory is
                        read by every Run of that Agent, so which Agent is
                        half of what is being decided. */}
                    <Typography.Text strong>{named.get(row.agent_id) ?? row.agent_id}</Typography.Text>
                    <Typography.Text type="secondary">{row.origin}</Typography.Text>
                    <Typography.Text type="secondary">{moment(row.updated_at)}</Typography.Text>
                  </Space>
                  {/* The proposal itself, unedited and uneditable. */}
                  <pre className="skill-file-body">{row.body}</pre>
                  {row.status === "pending" ? <Space wrap>
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
                  </Space> : row.kind === "shared" && row.status === "active" ? <Space wrap>
                    <Button onClick={() => { setEditing(row); setBody(row.body); change.reset(); }}>{t("memoryEdit")}</Button>
                    <Button danger loading={change.isPending} onClick={() => modal.confirm({ title: t("memoryRetire"), content: t("memoryRetireHint"), okText: t("confirm"), cancelText: t("cancel"), onOk: () => { change.mutate({ row, retire: true }); } })}>{t("memoryRetire")}</Button>
                  </Space> : null}
                </Space>
              </Card>
            ))}
          </Space>
        )}
      </Card>
      {view !== "pending" && <Space className="page-alert"><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>{t("previousPage")}</Button><Button disabled={!pending.data?.has_more} onClick={() => setOffset(offset + 20)}>{t("nextPage")}</Button></Space>}
      <Modal open={editing !== null} title={t("memoryEdit")} okText={t("saveName")} cancelText={t("cancel")} confirmLoading={change.isPending} okButtonProps={{ disabled: !body.trim() }} onCancel={() => setEditing(null)} onOk={() => editing && change.mutate({ row: editing, retire: false })}>
        <Typography.Paragraph>{t("writeSharedHint")}</Typography.Paragraph><Input.TextArea aria-label={t("memoryBody")} rows={6} maxLength={500} showCount value={body} onChange={(event) => setBody(event.target.value)} />
        {change.isError && <Alert type="error" title={problemMessage(change.error, t)} description={t("memoryConflictHint")} />}
      </Modal>
      {change.isError && !editing && <Alert type="error" title={problemMessage(change.error, t)} />}
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
