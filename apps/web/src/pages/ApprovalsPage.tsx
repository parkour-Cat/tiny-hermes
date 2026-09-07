import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Radio, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { useMyRole } from "../workspace/useMyRole";
import { problemMessage } from "../api/messages";
import type { ApprovalResponse, ApprovalsPageResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { EmptyState } from "../ui/EmptyState";
import { shortenId } from "../tables/ShortId";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/** Expired requests belong in history even though nobody decided them. */
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

export function ApprovalsPage({ focusId }: { focusId?: string }) {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const auth = useAuth();
  const { role, loading: roleLoading } = useMyRole();
  const canReview = auth.user?.is_platform_admin === true || role === "workspace_admin" || role === "platform_admin";
  const [chosenView, setChosenView] = useState<string | null>(null);
  const view = chosenView ?? (canReview ? "actionable" : "waiting");
  const [pendingOffset, setPendingOffset] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const pendingType = view === "actionable" ? "governance_approval" : canReview ? "user_confirmation" : null;
  const [modal, contextHolder] = Modal.useModal();
  const [rejecting, setRejecting] = useState<ApprovalResponse | null>(null);
  const [form] = Form.useForm<{ reason: string }>();
  const [error, setError] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };

  const approvals = useQuery({
    queryKey: ["approvals", workspaceId, pendingType, pendingOffset, focusId] as const,
    queryFn: async () => {
      if (focusId) return { items: [await api<ApprovalResponse>(`/api/v1/approvals/${focusId}`, scope)], has_more: false };
      const query = new URLSearchParams({ limit: "20", offset: String(pendingOffset) });
      if (pendingType !== null) query.set("approval_type", pendingType);
      return api<ApprovalsPageResponse>(`/api/v1/approvals?${query}`, scope);
    },
    enabled: workspaceId !== null && view !== "history" && (canReview || role !== null),
  });

  const [historyChoice, setHistoryChoice] = useState("all");
  const historyStatus =
    HISTORY_CHOICES.find((choice) => choice.key === historyChoice)?.statuses ?? DECIDED;
  const history = useQuery({
    queryKey: ["approvals", "history", workspaceId, historyStatus, historyOffset] as const,
    queryFn: () => {
      const query = new URLSearchParams();
      for (const status of historyStatus) query.append("status", status);
      query.set("order", "newest_first");
      query.set("limit", "20");
      query.set("offset", String(historyOffset));
      return api<ApprovalsPageResponse>(`/api/v1/approvals?${query.toString()}`, scope);
    },
    enabled: workspaceId !== null && view === "history",
  });

  const decide = useMutation({
    mutationFn: (input: { id: string; decision: "approve" | "reject"; reason?: string }) =>
      api<ApprovalResponse>(`/api/v1/approvals/${input.id}/decision`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ decision: input.decision, reason: input.reason ?? null }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["inbox-count", workspaceId] });
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

  const waiting = (approvals.data?.items ?? []).filter((item) => focusId ? item.id === focusId : item.status === "pending" && (pendingType === null || item.approval_type === pendingType));

  function card(approval: ApprovalResponse) {
    return (
      <Card key={approval.id} variant="borderless" className="page-alert approval-request">
        <Descriptions
          size="small"
          column={1}
          items={[
            { key: "outcome", label: t("approvalOutcome"), children: statusLabel(approval.status, t) },
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
        <Typography.Paragraph><strong>{t("approvalTarget")}: </strong>{typeof approval.document.target === "string" ? approval.document.target : t("approvalTargetUnspecified")}</Typography.Paragraph>
        <Typography.Paragraph>{t("approvalRequestParameters")}</Typography.Paragraph>
        <pre className="skill-file-body">{JSON.stringify(approval.document.arguments ?? {}, null, 2)}</pre>
        {/* The whole normalized document, not just its arguments. What is
            hashed includes the target and the permission, and a call with no
            arguments — which is most of them — would otherwise be reviewed as
            an empty object. A reviewer who cannot see the request cannot
            approve it. */}
        <details className="approval-details"><summary>{t("approvalFullRequest")}</summary><pre className="skill-file-body">{JSON.stringify(approval.document, null, 2)}</pre></details>
        {canReview && approval.status === "pending" && approval.approval_type === "governance_approval" && view === "actionable" ? <Space wrap>
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
        </Space> : <Typography.Paragraph type="secondary">{approval.status !== "pending" ? statusLabel(approval.status, t) : t(approval.approval_type === "user_confirmation" ? "approvalWaitUser" : "approvalWaitAdmin")}</Typography.Paragraph>}
      </Card>
    );
  }

  return (
    <>
      {contextHolder}
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}

      {!focusId && <Radio.Group
        aria-label={t("approvalQueueView")} value={view}
        onChange={(event) => { setChosenView(String(event.target.value)); setPendingOffset(0); }}
        className="page-alert"
        options={[
          ...(canReview ? [{ value: "actionable", label: t("approvalMyQueue") }] : []),
          { value: "waiting", label: t("approvalOthersQueue") },
          { value: "history", label: t("approvalHistoryQueue") },
        ]}
      />}
      {!canReview && role === null && !roleLoading ? <Alert type="warning" title={t("approvalRoleUnknown")} /> : null}
      {view !== "history" && <Card variant="borderless" loading={approvals.isLoading || (!canReview && roleLoading)}>
        {!focusId && <Typography.Paragraph type="secondary">{t(view === "actionable" ? "approvalMyQueueHint" : "approvalOthersQueueHint")}</Typography.Paragraph>}
        {approvals.isError ? <Alert type="error" title={problemMessage(approvals.error, t)} action={<Button onClick={() => void approvals.refetch()}>{t("retry")}</Button>} /> : !canReview && role === null ? null : waiting.length === 0 ? <EmptyState title={t("emptyApprovals")} /> : waiting.map(card)}
        {(pendingOffset > 0 || approvals.data?.has_more) && <Space className="page-alert">
          <Button disabled={pendingOffset === 0 || approvals.isFetching} onClick={() => setPendingOffset(Math.max(0, pendingOffset - 20))}>{t("approvalPreviousPage")}</Button>
          <Button disabled={!approvals.data?.has_more || approvals.isFetching} onClick={() => setPendingOffset(pendingOffset + 20)}>{t("approvalNextPage")}</Button>
        </Space>}
      </Card>}

      {view === "history" && <Card
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
            onChange={(event) => { setHistoryChoice(String(event.target.value)); setHistoryOffset(0); }}
            options={HISTORY_CHOICES.map((choice) => ({
              value: choice.key,
              label: choice.key === "all" ? t("approvalsHistoryAll") : statusLabel(choice.key, t),
            }))}
          />
          {history.isError ? (
            <Alert type="error" showIcon title={problemMessage(history.error, t)} action={<Button onClick={() => void history.refetch()}>{t("retry")}</Button>} />
          ) : null}
          {(history.data?.items ?? []).length === 0 && history.isSuccess ? (
            <EmptyState title={t("approvalsHistoryEmpty")} />
          ) : (
            <Table<ApprovalResponse>
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ x: 800 }}
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
                  // A link, so still a link — but the uuid is truncated and whole
                  // on hover (§4.1); the run page is one click away for the rest.
                  render: (value: string) => (
                    <Link to={`/workspaces/${workspaceId}/runs/${value}`} title={value}>
                      {shortenId(value)}
                    </Link>
                  ),
                },
              ]}
            />
          )}
          {(historyOffset > 0 || history.data?.has_more) && <Space>
            <Button disabled={historyOffset === 0 || history.isFetching} onClick={() => setHistoryOffset(Math.max(0, historyOffset - 20))}>{t("approvalPreviousPage")}</Button>
            <Button disabled={!history.data?.has_more || history.isFetching} onClick={() => setHistoryOffset(historyOffset + 20)}>{t("approvalNextPage")}</Button>
          </Space>}
        </Space>
      </Card>}

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
