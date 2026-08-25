import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Form, InputNumber, Modal, Space, Tag, Timeline, Typography } from "antd";
import type { DescriptionsProps } from "antd";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { downloadArtifact } from "../api/artifacts";
import { ApiError, api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  ArtifactResponse,
  BudgetDocument,
  CanonicalMessage,
  RunResponse,
  RunTreeResponse,
} from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { RUN_ACTIONS } from "../runs/actions";
import { eventNote, fill, outcomeLabel, statusNote } from "../runs/explain";
import { artifactIdsIn, mergeArtifacts, toolsOf, transcriptLineOf } from "../runs/transcript";
import { runQueryOptions, useRunEvents } from "../runs/useRunEvents";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/** Consumed against its limit, in the unit the limit is written in. */
function against(consumed: number, limit: number | null, unlimited: string): string {
  return `${consumed} / ${limit === null ? unlimited : limit}`;
}

/**
 * What this Run has spent, and how far that can be trusted.
 *
 * A null amount is rendered as the word for "unknown", never as a zero. That
 * is the whole of §12.4 as a person sees it: an endpoint nobody priced and an
 * endpoint priced at nothing are different facts, and a console that showed
 * `0` for both would be the place the distinction died.
 */
function costOf(budget: BudgetDocument, t: (key: MessageKey) => string): string {
  if (budget.consumed_cost === null) {
    return t("costUnknown");
  }
  const spent = `${budget.consumed_cost} ${budget.cost_currency ?? ""}`.trim();
  const limit =
    budget.max_cost === null ? t("budgetUnlimited") : `${budget.max_cost}`;
  return `${spent} / ${limit}`;
}

type Rows = NonNullable<DescriptionsProps["items"]>;

/** The same three words a person already sees beside token counts. */
const COST_QUALITY: Record<string, MessageKey> = {
  provider: "costProvider",
  estimated: "costEstimated",
  unknown: "costUnknownLabel",
};

function budgetRows(budget: BudgetDocument, t: (key: MessageKey) => string): Rows {
  return [
    {
      key: "execution",
      label: t("budgetExecution"),
      children: against(budget.consumed_execution_ms / 1000, budget.max_execution_seconds, t("budgetUnlimited")),
    },
    {
      key: "elapsed",
      label: t("budgetElapsedDeadline"),
      children: moment(budget.elapsed_deadline_at),
    },
    {
      key: "model-calls",
      label: t("budgetModelCalls"),
      children: against(budget.consumed_model_calls, budget.max_model_calls, t("budgetUnlimited")),
    },
    {
      key: "tool-calls",
      label: t("budgetToolCalls"),
      children: against(budget.consumed_tool_calls, budget.max_tool_calls, t("budgetUnlimited")),
    },
    {
      key: "tokens",
      label: t("budgetTokens"),
      children: against(budget.consumed_tokens, budget.max_tokens, t("budgetUnlimited")),
    },
    {
      key: "derived-retries",
      label: t("budgetDerivedRetries"),
      children: against(budget.derived_retry_count, budget.max_derived_retries, t("budgetUnlimited")),
    },
    {
      key: "cost",
      label: t("budgetCost"),
      children: (
        <Space size="small" wrap>
          <span>{costOf(budget, t)}</span>
          {/* The provenance is always shown, never only when it is bad: a
              figure whose origin appears sometimes is one a reader stops
              looking for. The words are the same three used beside token
              counts. */}
          <Tag>{t(COST_QUALITY[budget.cost_quality] ?? "costUnknownLabel")}</Tag>
        </Space>
      ),
    },
  ];
}

/** A relation in the reader's language, falling back to its own name. */
function relationKey(
  relation: string,
): "taskTreeRoot" | "taskTreeChild" | "taskTreeRetry" {
  switch (relation) {
    case "child":
      return "taskTreeChild";
    case "retry":
      return "taskTreeRetry";
    default:
      return "taskTreeRoot";
  }
}

export function RunDetailPage() {
  const t = useT();
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
  const scope = { workspace: workspaceId ?? "" };

  const snapshot = useQuery({ ...options, enabled });
  const events = useRunEvents({ runId: enabled ? runId : null, workspaceId });
  const sessionId = snapshot.data?.session_id;
  const messages = useQuery({
    queryKey: ["session-messages", workspaceId, sessionId] as const,
    queryFn: () =>
      api<CanonicalMessage[]>(`/api/v1/sessions/${sessionId ?? ""}/messages`, scope),
    enabled: enabled && sessionId !== undefined,
  });
  const artifacts = useQuery({
    queryKey: ["run-artifacts", workspaceId, runId] as const,
    queryFn: () => api<ArtifactResponse[]>(`/api/v1/runs/${runId}/artifacts`, scope),
    enabled,
  });
  // Its own request rather than fields on the Run: the tree is the same
  // answer from every node, so asking per node would fetch it once for each
  // link the page draws.
  const tree = useQuery({
    queryKey: ["run-tree", workspaceId, runId] as const,
    queryFn: () => api<RunTreeResponse>(`/api/v1/runs/${runId}/tree`, scope),
    enabled,
  });

  const [widening, setWidening] = useState(false);
  const [widenForm] = Form.useForm<{ maxModelCalls: number }>();
  const widen = useMutation({
    mutationFn: (input: { maxModelCalls: number; expected: number }) =>
      api<RunResponse>(`/api/v1/runs/${runId}/budget`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({
          expected_state_version: input.expected,
          max_model_calls: input.maxModelCalls,
        }),
      }),
    onSuccess: (updated) => {
      setWidening(false);
      widenForm.resetFields();
      queryClient.setQueryData(options.queryKey, updated);
      // The ceiling belongs to the tree, so every Run under it just changed.
      void queryClient.invalidateQueries({ queryKey: ["run-tree"] });
    },
    onError: (caught) => {
      widenForm.setFields([
        { name: "maxModelCalls", errors: [problemMessage(caught, t)] },
      ]);
    },
  });

  useEffect(() => {
    if (snapshot.data?.finished_at !== null && snapshot.data?.finished_at !== undefined) {
      void queryClient.invalidateQueries({ queryKey: ["session-messages", workspaceId, sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["run-artifacts", workspaceId, runId] });
    }
  }, [snapshot.data?.finished_at, queryClient, workspaceId, sessionId, runId]);

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
      const done = RUN_ACTIONS[action]?.done;
      setNote(done === undefined || done === null ? null : t(done));
    },
    onError: async (caught) => {
      setNote(null);
      setActionError(problemMessage(caught, t));
      if (caught instanceof ApiError && caught.code === "state_version_conflict") {
        await snapshot.refetch();
      }
    },
  });

  const retry = useMutation({
    mutationFn: () => {
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
      setActionError(problemMessage(caught, t));
    },
  });

  if (snapshot.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(snapshot.error, t)}
        action={<Button onClick={() => void snapshot.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }
  if (snapshot.data === undefined) {
    return <Card loading variant="borderless" />;
  }

  const run = snapshot.data;
  const turns = messages.data ?? [];
  const rounds = toolsOf(turns);
  const files = mergeArtifacts(
    artifacts.data ?? [],
    turns.flatMap((message) =>
      message.parts.flatMap((part) => (part.output === undefined ? [] : artifactIdsIn(part.output))),
    ),
  );

  function act(action: string): void {
    const offer = RUN_ACTIONS[action];
    if (offer === undefined) {
      return;
    }
    const send = (): Promise<unknown> =>
      action === "retry"
        ? retry.mutateAsync()
        : control.mutateAsync({ action, expected: run.state_version });
    if (offer.question === null) {
      void send().catch(() => undefined);
      return;
    }
    void modal.confirm({
      title: t(offer.label),
      content: t(offer.question),
      okText: t("confirm"),
      cancelText: t("cancel"),
      onOk: () => send().catch(() => undefined),
    });
  }

  function runLink(id: string) {
    return <Link to={`/workspaces/${workspaceId ?? ""}/runs/${id}`}>{id}</Link>;
  }

  const treeNodes = tree.data?.nodes ?? [];
  // A Run alone in its tree is most Runs. A card saying so would be noise on
  // nearly every page in the console.
  const hasTree = treeNodes.length > 1;

  const situation = statusNote(run);
  const outcome = outcomeLabel(run.goal.outcome);

  const facts: Rows = [
    { key: "status", label: t("runStatus"), children: <Tag>{run.status}</Tag> },
    { key: "queue", label: t("runQueue"), children: run.queue.status },
    {
      key: "goal-round",
      label: t("runGoalRound"),
      children: run.goal.round === null ? t("runGoalNotJudged") : run.goal.round,
    },
    {
      key: "goal-outcome",
      label: t("runGoalOutcome"),
      // The raw value when this console has no word for it: a verdict it does
      // not recognise is still the reason the Run is where it is.
      children:
        outcome === null ? (run.goal.outcome ?? t("runGoalNotJudged")) : t(outcome),
    },
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
  if (run.retry_of_run_id !== null) {
    facts.push({ key: "retry-of", label: t("retryOfRun"), children: runLink(run.retry_of_run_id) });
  }
  if (run.parent_run_id !== null) {
    // Only for a Run that was delegated. Shown as a link because "who asked
    // for this" is the first thing somebody looking at a child wants, and a
    // child holds a Session of its own so nothing else on the page says it.
    facts.push({
      key: "parent",
      label: t("runParent"),
      children: runLink(run.parent_run_id),
    });
    facts.push({ key: "depth", label: t("runDepth"), children: String(run.depth) });
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
  if (run.goal.unmet.length > 0) {
    facts.push({
      key: "goal-unmet",
      label: t("runGoalUnmet"),
      children: run.goal.unmet.join("; "),
    });
  }

  const timeline = events.entries.map((entry) => {
    if (entry.kind === "gap") {
      return {
        key: `gap-${entry.after}`,
        color: "red",
        children: (
          <Typography.Text type="danger">
            {`${t("eventGapPrefix")}${entry.missing}${t("eventGapSuffix")}`}
          </Typography.Text>
        ),
      };
    }
    const said = eventNote(entry.frame);
    return {
      key: `event-${entry.frame.sequence}`,
      children: (
        <>
          <Space size="middle" wrap>
            <Typography.Text strong>{entry.frame.event_type}</Typography.Text>
            <Typography.Text type="secondary">{`#${entry.frame.sequence}`}</Typography.Text>
            <Typography.Text type="secondary">{moment(entry.frame.occurred_at)}</Typography.Text>
            <details>
              <summary>{t("eventPayload")}</summary>
              <pre>{JSON.stringify(entry.frame.payload, null, 2)}</pre>
            </details>
          </Space>
          {said === null ? null : (
            <Typography.Paragraph className="fact-note">
              {fill(t(said.key), said.values)}
            </Typography.Paragraph>
          )}
        </>
      ),
    };
  });

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("runDetailTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{run.id}</Typography.Paragraph>
        </div>
        <Space wrap>
          {run.available_actions.includes("widen_budget") && (
            // Not routed through `RUN_ACTIONS`: every entry in that table is
            // a bare POST, and this one carries a number a person has to
            // choose. A generic button here would send a guess.
            <Button onClick={() => setWidening(true)}>{t("widenBudget")}</Button>
          )}
          {run.available_actions.map((action) => {
            const offer = RUN_ACTIONS[action];
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
        <Alert className="page-alert" type="warning" title={problemMessage(events.error, t)} showIcon />
      )}
      {situation === null ? null : (
        <Alert className="page-alert" type="info" title={t(situation)} showIcon />
      )}
      <Card title={t("summarySection")} variant="borderless" className="page-alert">
        <Descriptions column={{ xs: 1, sm: 2 }} size="small" items={facts} />
        <Typography.Title level={5}>{t("budgetSection")}</Typography.Title>
        {hasTree && (
          // §669: the whole tree spends one budget, and these numbers come
          // from `run_budget_scopes` by `budget_root_run_id` — they are the
          // tree's on every node. Unlabelled, a child's page reports the
          // tree's spend as this Run's.
          <Typography.Paragraph type="secondary">{t("budgetSharedNote")}</Typography.Paragraph>
        )}
        <Descriptions column={{ xs: 1, sm: 2 }} size="small" items={budgetRows(run.budget, t)} />
      </Card>
      {hasTree && (
        // §952's 完整父子任务树, drawn from whichever node was opened. A
        // child used to carry only `parent_run_id`, so somebody reading the
        // child that failed could not see that its siblings succeeded.
        <Card title={t("taskTreeSection")} variant="borderless" className="page-alert">
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Typography.Paragraph type="secondary">{t("taskTreeIntro")}</Typography.Paragraph>
            {treeNodes.map((node) => (
              <Space key={node.id} size="small" style={{ paddingLeft: node.depth * 24 }}>
                <Tag>{node.status}</Tag>
                <Tag>{t(relationKey(node.relation))}</Tag>
                {node.id === runId ? (
                  // Marked rather than left to the reader to match ids: a
                  // column of uuids does not say which one is open.
                  <Typography.Text strong>{t("taskTreeYouAreHere")}</Typography.Text>
                ) : (
                  runLink(node.id)
                )}
              </Space>
            ))}
          </Space>
        </Card>
      )}
      <Card title={t("messagesSection")} variant="borderless" className="page-alert">
        {turns.length === 0 ? (
          <Empty description={t("emptyMessages")} />
        ) : (
          /* `index` as the key is safe here and only here: this list is
             read-only, append-only and never reordered, and a message
             carries no id of its own. It stops being safe the moment a turn
             can be edited or inserted. */
          turns.map((message, index) => (
            <article className="workspace-row" key={`${message.role}-${index}`}>
              <Tag>{message.role}</Tag>
              <Typography.Paragraph className="fact-note transcript-line">
                {transcriptLineOf(message)}
              </Typography.Paragraph>
            </article>
          ))
        )}
      </Card>
      <Card title={t("toolsCallsSection")} variant="borderless" className="page-alert">
        {rounds.length === 0 ? (
          <Empty description={t("emptyTools")} />
        ) : (
          rounds.map((round) => (
            <article className="workspace-row" key={round.callId || round.name}>
              <Typography.Text strong>{round.name}</Typography.Text>
              <div>
                <Typography.Paragraph className="fact-note">
                  {JSON.stringify(round.arguments)}
                </Typography.Paragraph>
                <Typography.Paragraph type="secondary">{round.output}</Typography.Paragraph>
                {round.artifactIds.map((id) => (
                  <Button
                    key={id}
                    onClick={() =>
                      void downloadArtifact(id, id, workspaceId ?? "").catch((caught) =>
                        setActionError(problemMessage(caught, t)),
                      )
                    }
                  >
                    {t("downloadArtifact")}
                  </Button>
                ))}
              </div>
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
                    setActionError(problemMessage(caught, t)),
                  )
                }
              >
                {t("downloadArtifact")}
              </Button>
            </Space>
          ))
        )}
      </Card>
      <Card title={t("timelineSection")} variant="borderless">
        {timeline.length === 0 ? (
          <Empty description={t("emptyTimeline")} />
        ) : (
          <Timeline items={timeline} />
        )}
      </Card>
      <Modal
        open={widening}
        title={t("widenBudget")}
        okText={t("confirm")}
        cancelText={t("cancel")}
        confirmLoading={widen.isPending}
        onCancel={() => setWidening(false)}
        onOk={() => void widenForm.submit()}
      >
        <Form<{ maxModelCalls: number }>
          form={widenForm}
          layout="vertical"
          requiredMark={false}
          initialValues={{ maxModelCalls: run.budget.max_model_calls }}
          onFinish={(values) =>
            widen.mutate({
              maxModelCalls: values.maxModelCalls,
              expected: run.state_version,
            })
          }
        >
          <Typography.Paragraph type="secondary">
            {/* Said here, not only on the budget card: the person raising a
                ceiling is deciding for every Run under this budget root. */}
            {t("widenBudgetHint")}
          </Typography.Paragraph>
          <Form.Item
            name="maxModelCalls"
            label={t("widenBudgetField")}
            rules={[
              {
                validator: (_rule, value: number) =>
                  typeof value === "number" && value > run.budget.max_model_calls
                    ? Promise.resolve()
                    : // Checked here rather than left to the API, which
                      // refuses it *and* writes `run.budget_widen_denied`:
                      // a typo should not spend an audit row.
                      Promise.reject(new Error(t("widenBudgetMustRise"))),
              },
            ]}
          >
            <InputNumber min={1} style={{ width: "100%" }} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
