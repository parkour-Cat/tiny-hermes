import { useQuery } from "@tanstack/react-query";
import { Card, Empty, Space, Statistic, Table, Tag, Typography } from "antd";

import { api } from "../api/client";
import type { UsageByQualityResponse, UsageSummaryResponse } from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/** The same three words a person already sees beside a single Run's cost
 * (`RunDetailPage`'s own `COST_QUALITY`) — one vocabulary for one idea. */
const COST_QUALITY: Record<string, MessageKey> = {
  provider: "costProvider",
  estimated: "costEstimated",
  unknown: "costUnknownLabel",
};

/** A null amount renders as the word for "unknown", never a `0` — the same
 * rule a single Run's cost follows, carried up to the workspace rollup. */
function costCell(bucket: UsageByQualityResponse, t: (key: MessageKey) => string): string {
  if (bucket.consumed_cost === null) {
    return t("costUnknown");
  }
  return `${bucket.consumed_cost} ${bucket.cost_currency ?? ""}`.trim();
}

/**
 * A workspace's usage, one row per `cost_quality`.
 *
 * §6's own requirement is that the quality split stay visible rather than
 * becoming a footnote — a page that showed one blended cost figure would be
 * the exact failure this view exists to avoid. So there is no single "total
 * cost" anywhere here: the totals row above the table carries only the
 * counters that mean the same thing regardless of quality — call counts and
 * token counts — and every cost figure lives inside its own row, tagged
 * with how far it can be trusted.
 */
export function UsagePage() {
  const t = useT();
  const workspaceId = useWorkspaceId();

  const usage = useQuery({
    queryKey: ["usage-summary", workspaceId] as const,
    queryFn: () =>
      api<UsageSummaryResponse>("/api/v1/usage", { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null,
  });

  const data = usage.data;
  const buckets = data?.by_cost_quality ?? [];

  return (
    <Card title={t("usage")} loading={usage.isPending}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Paragraph type="secondary">{t("usageIntro")}</Typography.Paragraph>

        {data === undefined ? null : (
          <Space size="large" wrap>
            <Statistic title={t("usageTotalRuns")} value={data.total_run_count} />
            <Statistic title={t("budgetModelCalls")} value={data.total_model_calls} />
            <Statistic title={t("budgetToolCalls")} value={data.total_tool_calls} />
            <Statistic title={t("budgetTokens")} value={data.total_tokens} />
          </Space>
        )}

        {buckets.length === 0 && !usage.isLoading ? (
          <Empty description={t("usageEmpty")} />
        ) : (
          <Table<UsageByQualityResponse>
            rowKey="cost_quality"
            dataSource={buckets}
            pagination={false}
            columns={[
              {
                title: t("usageQualityColumn"),
                dataIndex: "cost_quality",
                render: (value: string) => (
                  <Tag>{t(COST_QUALITY[value] ?? "costUnknownLabel")}</Tag>
                ),
              },
              {
                title: t("budgetCost"),
                key: "cost",
                render: (_: unknown, bucket: UsageByQualityResponse) => costCell(bucket, t),
              },
              { title: t("usageRunCountColumn"), dataIndex: "run_count" },
              { title: t("budgetModelCalls"), dataIndex: "consumed_model_calls" },
              { title: t("budgetToolCalls"), dataIndex: "consumed_tool_calls" },
              { title: t("budgetTokens"), dataIndex: "consumed_tokens" },
            ]}
          />
        )}
      </Space>
    </Card>
  );
}
