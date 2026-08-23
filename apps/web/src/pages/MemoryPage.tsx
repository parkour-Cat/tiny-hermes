import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Space, Tag, Typography } from "antd";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, MemoryResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
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
          <Typography.Title level={2}>{t("memoryReview")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("memoryReviewIntro")}</Typography.Paragraph>
        </div>
      </div>

      <Card loading={pending.isPending} variant="borderless">
        {rows.length === 0 ? (
          <Empty description={t("memoryEmpty")} />
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
