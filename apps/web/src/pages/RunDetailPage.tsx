import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Modal, Space, Tag, Timeline, Typography } from "antd";
import type { DescriptionsProps } from "antd";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { BudgetDocument, RunResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { t } from "../i18n/zh-CN";
import type { MessageKey } from "../i18n/zh-CN";
import { runQueryOptions, useRunEvents } from "../runs/useRunEvents";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/**
 * What the console offers for each action the platform reports.
 *
 * Keyed by the action names the state machine produces. An action this table
 * does not know is not rendered: its request shape would be a guess, and a
 * button that posts a guess is worse than one the user never sees.
 */
type Offer = {
  label: MessageKey;
  /** What to ask before sending, when the action is worth asking about. */
  question: MessageKey | null;
  /** What to say afterwards — a request submitted, never a state reached. */
  done: MessageKey | null;
};

const ACTIONS: Partial<Record<string, Offer>> = {
  pause: { label: "pauseRun", question: null, done: "pauseRequested" },
  resume: { label: "resumeRun", question: null, done: "resumeRequested" },
  cancel: { label: "cancelRun", question: "cancelRunWarning", done: "cancelRequested" },
  // A retry answers with a different Run, so the page moves rather than
  // reporting anything about this one.
  retry: { label: "retryRun", question: "retryRunWarning", done: null },
};

/** Consumed against its limit, in the unit the limit is written in. */
function against(consumed: number, limit: number | null): string {
  return `${consumed} / ${limit === null ? t("budgetUnlimited") : limit}`;
}

type Rows = NonNullable<DescriptionsProps["items"]>;

function budgetRows(budget: BudgetDocument): Rows {
  return [
    {
      key: "execution",
      label: t("budgetExecution"),
      // The platform counts execution in milliseconds and bounds it in
      // seconds. Printed side by side unconverted, the pair would read as a
      // run using two hundred times its allowance.
      children: against(budget.consumed_execution_ms / 1000, budget.max_execution_seconds),
    },
    {
      key: "elapsed",
      label: t("budgetElapsedDeadline"),
      children: moment(budget.elapsed_deadline_at),
    },
    {
      key: "model-calls",
      label: t("budgetModelCalls"),
      children: against(budget.consumed_model_calls, budget.max_model_calls),
    },
    {
      key: "tool-calls",
      label: t("budgetToolCalls"),
      children: against(budget.consumed_tool_calls, budget.max_tool_calls),
    },
    {
      key: "tokens",
      label: t("budgetTokens"),
      children: against(budget.consumed_tokens, budget.max_tokens),
    },
    {
      key: "derived-retries",
      label: t("budgetDerivedRetries"),
      children: against(budget.derived_retry_count, budget.max_derived_retries),
    },
  ];
}

export function RunDetailPage() {
  const workspaceId = useWorkspaceId();
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [modal, contextHolder] = Modal.useModal();
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const retryKey = useRef<string | null>(null);
  const enabled = workspaceId !== null && runId !== "";
  const options = runQueryOptions(workspaceId ?? "", runId);

  const snapshot = useQuery({ ...options, enabled });
  const events = useRunEvents({ runId: enabled ? runId : null, workspaceId });

  const control = useMutation({
    mutationFn: ({ action, expected }: { action: string; expected: number }) =>
      api<RunResponse>(`/api/v1/runs/${runId}/${action}`, {
        workspace: workspaceId ?? "",
        method: "POST",
        body: JSON.stringify({ expected_state_version: expected }),
      }),
    onSuccess: (updated, { action }) => {
      queryClient.setQueryData(options.queryKey, updated);
      setActionError(null);
      const done = ACTIONS[action]?.done;
      setNote(done === undefined || done === null ? null : t(done));
    },
    onError: async (caught) => {
      setNote(null);
      setActionError(problemMessage(caught));
      // The opposite of the draft editor's conflict handling, and for the
      // opposite reason: nothing the user typed is at risk here, so the honest
      // move is to show them where the Run actually is and let them choose
      // again against the buttons that go with it.
      if (caught instanceof ApiError && caught.code === "state_version_conflict") {
        await snapshot.refetch();
      }
    },
  });

  const retry = useMutation({
    mutationFn: () => {
      // One key for one intent to retry. A fresh key on a second press would
      // derive a second Run from the same failure.
      retryKey.current ??= crypto.randomUUID();
      return api<RunResponse>(`/api/v1/runs/${runId}/retry`, {
        workspace: workspaceId ?? "",
        method: "POST",
        headers: { "Idempotency-Key": retryKey.current },
      });
    },
    onSuccess: (derived) => {
      retryKey.current = null;
      setActionError(null);
      navigate(`/workspaces/${workspaceId ?? ""}/runs/${derived.id}`);
    },
    onError: (caught) => {
      setNote(null);
      setActionError(problemMessage(caught));
    },
  });

  if (snapshot.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(snapshot.error)}
        action={<Button onClick={() => void snapshot.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }
  if (snapshot.data === undefined) {
    return <Card loading variant="borderless" />;
  }

  const run = snapshot.data;

  function act(action: string): void {
    const offer = ACTIONS[action];
    if (offer === undefined) {
      return;
    }
    const send = (): Promise<unknown> =>
      action === "retry"
        ? retry.mutateAsync()
        : control.mutateAsync({ action, expected: run.state_version });
    if (offer.question === null) {
      // Pausing and resuming are requests the platform can decline and the
      // user can reverse; asking first would be ceremony.
      void send().catch(() => undefined);
      return;
    }
    void modal.confirm({
      title: t(offer.label),
      content: t(offer.question),
      okText: t("confirm"),
      cancelText: t("cancel"),
      // The dialog closes either way: a refusal is reported on the page, and
      // an open dialog would cover the message the user has to read.
      onOk: () => send().catch(() => undefined),
    });
  }

  function runLink(id: string) {
    return <Link to={`/workspaces/${workspaceId ?? ""}/runs/${id}`}>{id}</Link>;
  }

  const facts: Rows = [
    // The state machine's own name, as everywhere else in the console: a second
    // vocabulary between the user and the events they are reading is a drift
    // waiting to happen.
    { key: "status", label: t("runStatus"), children: <Tag>{run.status}</Tag> },
    { key: "queue", label: t("runQueue"), children: run.queue.status },
    { key: "session-sequence", label: t("runSessionSequence"), children: run.session_sequence },
    { key: "state-version", label: t("runStateVersion"), children: run.state_version },
    {
      key: "replay-safe",
      label: t("runCheckpointReplaySafe"),
      children: run.checkpoint_replay_safe ? t("yes") : t("no"),
    },
    {
      key: "effect",
      label: t("runCheckpointEffect"),
      children: run.checkpoint_effect_status,
    },
    { key: "session", label: t("runSession"), children: run.session_id },
    { key: "agent-version", label: t("runAgentVersion"), children: run.agent_version_id },
    { key: "budget-root", label: t("budgetRootRun"), children: runLink(run.budget_root_run_id) },
    { key: "created", label: t("runCreatedAt"), children: moment(run.created_at) },
    {
      key: "started",
      label: t("runStartedAt"),
      children: run.started_at === null ? t("notFinished") : moment(run.started_at),
    },
    {
      key: "finished",
      label: t("runFinishedAt"),
      children: run.finished_at === null ? t("notFinished") : moment(run.finished_at),
    },
  ];
  // Rows that exist only when the platform has something to put in them. A row
  // reading "暂停原因 —" invites the reader to look for a reason there isn't.
  if (run.retry_of_run_id !== null) {
    facts.push({ key: "retry-of", label: t("retryOfRun"), children: runLink(run.retry_of_run_id) });
  }
  if (run.blocked_by_run_id !== null) {
    facts.push({
      key: "blocked-by",
      label: t("runBlockedBy"),
      children: runLink(run.blocked_by_run_id),
    });
  }
  if (run.pause_reason !== null) {
    facts.push({ key: "pause-reason", label: t("runPauseReason"), children: run.pause_reason });
  }
  if (run.wait_kind !== null) {
    facts.push({ key: "wait-kind", label: t("runWaitKind"), children: run.wait_kind });
  }
  if (run.wait_deadline_at !== null) {
    facts.push({
      key: "wait-deadline",
      label: t("runWaitDeadline"),
      children: moment(run.wait_deadline_at),
    });
  }

  const timeline = events.entries.map((entry) =>
    entry.kind === "gap"
      ? {
          key: `gap-${entry.after}`,
          color: "red",
          children: (
            <Typography.Text type="danger">
              {`${t("eventGapPrefix")}${entry.missing}${t("eventGapSuffix")}`}
            </Typography.Text>
          ),
        }
      : {
          key: `event-${entry.frame.sequence}`,
          children: (
            <Space size="middle" wrap>
              <Typography.Text strong>{entry.frame.event_type}</Typography.Text>
              <Typography.Text type="secondary">{`#${entry.frame.sequence}`}</Typography.Text>
              <Typography.Text type="secondary">{moment(entry.frame.occurred_at)}</Typography.Text>
            </Space>
          ),
        },
  );

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("runDetailTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{run.id}</Typography.Paragraph>
        </div>
        <Space wrap>
          {run.available_actions.map((action) => {
            const offer = ACTIONS[action];
            return offer === undefined ? null : (
              <Button
                key={action}
                loading={action === "retry" ? retry.isPending : control.isPending}
                onClick={() => act(action)}
              >
                {t(offer.label)}
              </Button>
            );
          })}
        </Space>
      </div>
      {note === null ? null : <Alert className="page-alert" type="info" title={note} showIcon />}
      {actionError === null ? null : (
        <Alert className="page-alert" type="warning" title={actionError} showIcon />
      )}
      {events.error === null ? null : (
        <Alert className="page-alert" type="warning" title={events.error} showIcon />
      )}
      <Card title={t("summarySection")} variant="borderless" className="page-alert">
        <Descriptions column={{ xs: 1, sm: 2 }} size="small" items={facts} />
        <Typography.Title level={5}>{t("budgetSection")}</Typography.Title>
        <Descriptions column={{ xs: 1, sm: 2 }} size="small" items={budgetRows(run.budget)} />
      </Card>
      <Card title={t("timelineSection")} variant="borderless">
        {timeline.length === 0 ? (
          <Empty description={t("emptyTimeline")} />
        ) : (
          <Timeline items={timeline} />
        )}
      </Card>
    </>
  );
}
