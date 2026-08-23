import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Form, Input, Modal, Radio, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { ApprovalResponse, ApprovalsPageResponse } from "../api/types";
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
 * §26's queue adds the third thing: reading back what was decided. It is a
 * section of its own rather than a status column on the cards above, because
 * an answered approval is not work — mixing it into a list a person works
 * through is how a decided row gets a second decision aimed at it.
 *
 * History is narrowed by the server, never here. Filtering a page the browser
 * happens to hold answers "none of the rows I fetched match" to somebody who
 * asked "did this ever happen", and those two look identical.
 *
 * There is no assignment: the product design names no assignee, and §4.6
 * already fixes who may decide. See `approval_routes.py`.
 */
/**
 * What "history" means by default: everything that is no longer waiting.
 *
 * `expired` belongs here as much as the two answers do. Nobody decided it,
 * and that is the fact worth reading — a queue that showed only approvals and
 * rejections would quietly drop the rows that timed out, which are the ones
 * §26's 审批负担 question is about.
 */
const DECIDED = ["approved", "rejected", "expired"];

/**
 * A status in the reader's language.
 *
 * Spelled out rather than built as `t(\`approvalStatus_${status}\`)`: a
 * template key type-checks against nothing, so a status the backend adds
 * later would render blank in every language and nobody would see a build
 * error. An unknown one falls back to its own name, which is at least true.
 */
function statusLabel(status: string, t: (key: "approvalStatus_pending" | "approvalStatus_approved" | "approvalStatus_rejected" | "approvalStatus_expired") => string): string {
  switch (status) {
    case "pending":
      return t("approvalStatus_pending");
    case "approved":
      return t("approvalStatus_approved");
    case "rejected":
      return t("approvalStatus_rejected");
    case "expired":
      return t("approvalStatus_expired");
    default:
      return status;
  }
}

/** What the history filter offers, and which statuses each choice asks for. */
const HISTORY_CHOICES: { key: string; statuses: string[] }[] = [
  { key: "all", statuses: DECIDED },
  { key: "approved", statuses: ["approved"] },
  { key: "rejected", statuses: ["rejected"] },
  { key: "expired", statuses: ["expired"] },
];

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
    queryFn: () => api<ApprovalsPageResponse>("/api/v1/approvals", scope),
    enabled: workspaceId !== null,
  });

  const [historyChoice, setHistoryChoice] = useState("all");
  const historyStatus =
    HISTORY_CHOICES.find((choice) => choice.key === historyChoice)?.statuses ?? DECIDED;
  const history = useQuery({
    queryKey: ["approvals", "history", workspaceId, historyStatus] as const,
    queryFn: () => {
      const query = new URLSearchParams();
      for (const status of historyStatus) query.append("status", status);
      query.set("order", "newest_first");
      return api<ApprovalsPageResponse>(`/api/v1/approvals?${query.toString()}`, scope);
    },
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
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  if (approvals.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(approvals.error, t)}
        action={<Button onClick={() => void approvals.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  const waiting = approvals.data?.items ?? [];
  const user = waiting.filter((item) => item.approval_type === "user_confirmation");
  const governance = waiting.filter((item) => item.approval_type === "governance_approval");

  function card(approval: ApprovalResponse) {
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
        {/* The whole normalized document, not just its arguments. What is
            hashed includes the target and the permission, and a call with no
            arguments — which is most of them — would otherwise be reviewed as
            an empty object. A reviewer who cannot see the request cannot
            approve it. */}
        <pre className="skill-file-body">{JSON.stringify(approval.document, null, 2)}</pre>
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

      <Card
        title={t("approvalsHistory")}
        variant="borderless"
        className="page-alert"
        loading={history.isPending}
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary">
            {t("approvalsHistoryIntro")}
          </Typography.Paragraph>
          <Radio.Group
            aria-label={t("approvalsHistoryStatus")}
            optionType="button"
            value={historyChoice}
            onChange={(event) => setHistoryChoice(String(event.target.value))}
            options={HISTORY_CHOICES.map((choice) => ({
              value: choice.key,
              label: choice.key === "all" ? t("approvalsHistoryAll") : statusLabel(choice.key, t),
            }))}
          />
          {history.isError ? (
            <Alert type="error" showIcon title={problemMessage(history.error, t)} />
          ) : null}
          {(history.data?.items ?? []).length === 0 && !history.isPending ? (
            <Empty description={t("approvalsHistoryEmpty")} />
          ) : (
            <Table<ApprovalResponse>
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={history.data?.items ?? []}
              columns={[
                {
                  title: t("approvalWhen"),
                  dataIndex: "decided_at",
                  render: (value: string | null) => (value === null ? "—" : moment(value)),
                },
                {
                  title: t("approvalTool"),
                  dataIndex: "tool",
                  render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
                },
                {
                  title: t("approvalOutcome"),
                  dataIndex: "status",
                  render: (value: string) => <Tag>{statusLabel(value, t)}</Tag>,
                },
                {
                  title: t("approvalDecidedBy"),
                  dataIndex: "decided_by",
                  // "—" rather than blank: an expired approval was decided by
                  // nobody, and that is different from a missing value.
                  render: (value: string | null) => value ?? "—",
                },
                {
                  title: t("approvalReason"),
                  dataIndex: "decision_reason",
                  render: (value: string | null) => value ?? "—",
                },
                {
                  title: t("approvalRun"),
                  dataIndex: "run_id",
                  render: (value: string) => (
                    <Link to={`/workspaces/${workspaceId}/runs/${value}`}>{value}</Link>
                  ),
                },
              ]}
            />
          )}
          {history.data?.has_more === true ? (
            // Said, not paged over: a table that ends at the page boundary
            // without a word reads as "that is all there was".
            <Typography.Paragraph type="secondary">
              {t("approvalsHistoryMore")}
            </Typography.Paragraph>
          ) : null}
        </Space>
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
