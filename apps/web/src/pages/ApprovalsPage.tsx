import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Form, Input, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { ApprovalResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/**
 * What is waiting for a person, with the two kinds kept apart.
 *
 * They are separated on the page because they are two different powers, not
 * two categories of one thing. §16.3 gives a `user_confirmation` to the end
 * user who started the Run and to nobody else; a `governance_approval` belongs
 * to an administrator and an end user may never answer one. A single merged
 * list would invite the reader to think of them as one queue they work
 * through, which is exactly the habit the section exists to prevent.
 *
 * Every row shows the **normalized call**, as the platform hashed it. A
 * reviewer deciding from a summary this console rewrote would be approving
 * something nobody can prove matches what runs.
 *
 * The full governance queue — filters, assignment, history — is M3's. This is
 * the part without which a write cannot happen at all: see it, approve it,
 * reject it, know why.
 */
export function ApprovalsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [modal, contextHolder] = Modal.useModal();
  const [rejecting, setRejecting] = useState<ApprovalResponse | null>(null);
  const [form] = Form.useForm<{ reason: string }>();
  const [error, setError] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };

  const approvals = useQuery({
    queryKey: ["approvals", workspaceId] as const,
    queryFn: () => api<ApprovalResponse[]>("/api/v1/approvals", scope),
    enabled: workspaceId !== null,
  });

  const decide = useMutation({
    mutationFn: (input: { id: string; decision: "approve" | "reject"; reason?: string }) =>
      api<ApprovalResponse>(`/api/v1/approvals/${input.id}/decision`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ decision: input.decision, reason: input.reason ?? null }),
      }),
    onSuccess: () => {
      setError(null);
      setRejecting(null);
      form.resetFields();
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
      // The Run moved too — back to the queue, or into a pause — so anything
      // showing it is stale the moment this returns.
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  if (approvals.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(approvals.error)}
        action={<Button onClick={() => void approvals.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  const waiting = approvals.data ?? [];
  const user = waiting.filter((item) => item.approval_type === "user_confirmation");
  const governance = waiting.filter((item) => item.approval_type === "governance_approval");

  function card(approval: ApprovalResponse) {
    const argumentsOf = approval.document.arguments;
    return (
      <Card key={approval.id} variant="borderless" className="page-alert">
        <Descriptions
          size="small"
          column={1}
          items={[
            { key: "tool", label: t("approvalTool"), children: <Typography.Text code>{approval.tool}</Typography.Text> },
            {
              key: "permission",
              label: t("approvalPermission"),
              children: approval.required_permission ?? "—",
            },
            { key: "expires", label: t("approvalExpires"), children: moment(approval.expires_at) },
            {
              key: "run",
              label: t("approvalRun"),
              children: (
                <Link to={`/workspaces/${workspaceId}/runs/${approval.run_id}`}>
                  {approval.run_id}
                </Link>
              ),
            },
          ]}
        />
        <Typography.Paragraph type="secondary">
          {t("approvalArgumentsHint")}
        </Typography.Paragraph>
        {/* The document verbatim. Rendered rather than summarized, because the
            hash covers this exact shape and a summary could not be checked
            against anything. */}
        <pre className="skill-file-body">
          {JSON.stringify(argumentsOf ?? approval.document, null, 2)}
        </pre>
        <Space wrap>
          <Button
            type="primary"
            loading={decide.isPending}
            onClick={() =>
              void modal.confirm({
                title: t("approvalApprove"),
                content: t("approvalApproveWarning"),
                okText: t("confirm"),
                cancelText: t("cancel"),
                onOk: () =>
                  decide.mutateAsync({ id: approval.id, decision: "approve" }).catch(() => undefined),
              })
            }
          >
            {t("approvalApprove")}
          </Button>
          <Button danger onClick={() => setRejecting(approval)}>
            {t("approvalReject")}
          </Button>
        </Space>
      </Card>
    );
  }

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("approvalsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("approvalsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}

      <Card
        title={
          <Space>
            {t("approvalsUser")}
            <Tag>{user.length}</Tag>
          </Space>
        }
        variant="borderless"
        className="page-alert"
        loading={approvals.isPending}
      >
        <Typography.Paragraph type="secondary">{t("approvalsUserIntro")}</Typography.Paragraph>
        {user.length === 0 ? <Empty description={t("emptyApprovals")} /> : user.map(card)}
      </Card>

      <Card
        title={
          <Space>
            {t("approvalsGovernance")}
            <Tag>{governance.length}</Tag>
          </Space>
        }
        variant="borderless"
        loading={approvals.isPending}
      >
        <Typography.Paragraph type="secondary">
          {t("approvalsGovernanceIntro")}
        </Typography.Paragraph>
        {governance.length === 0 ? (
          <Empty description={t("emptyApprovals")} />
        ) : (
          governance.map(card)
        )}
      </Card>

      <Modal
        open={rejecting !== null}
        title={t("approvalReject")}
        okText={t("approvalReject")}
        cancelText={t("cancel")}
        confirmLoading={decide.isPending}
        onCancel={() => setRejecting(null)}
        onOk={() => void form.submit()}
      >
        <Form<{ reason: string }>
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) =>
            rejecting === null
              ? undefined
              : decide.mutate({
                  id: rejecting.id,
                  decision: "reject",
                  reason: values.reason,
                })
          }
        >
          {/* Required, and the server requires it too. The person whose Run
              stopped is not the person who stopped it, and "no" with no reason
              gives them nothing to change. */}
          <Form.Item
            name="reason"
            label={t("approvalReason")}
            extra={t("approvalReasonRequired")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
