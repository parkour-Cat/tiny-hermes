import { useInfiniteQuery } from "@tanstack/react-query";
import { Alert, Button, Card, AutoComplete, Space, Table, Tag, Typography } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import { TimeRangeFilter, timeBounds } from "../ui/TimeRangeFilter";
import { useState } from "react";

import { api, download } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AuditEventsPageResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useLocale, useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { EmptyState } from "../ui/EmptyState";

/**
 * The trail, and — just as importantly — how much of it this reader is
 * being shown.
 *
 * §4.6 gives five subjects five different ranges over `audit_events`, three
 * of them partial. Two of those three are invisible from the rows alone: a
 * redacted `context` arrives as `{}`, which is indistinguishable from a row
 * that never carried one, and a developer's narrowed result is simply a
 * shorter list. A page that rendered either without saying so would be
 * handing somebody incomplete evidence with no sign that it was incomplete
 * — and the reader most likely to act on that is the one investigating an
 * incident, who has every reason to read a short list as "it didn't
 * happen".
 *
 * So the banner is not decoration. It is the part of this page that keeps a
 * partial answer from being mistaken for the whole one.
 */
export function AuditPage() {
  const t = useT();
  const { locale } = useLocale();
  const workspaceId = useWorkspaceId();
  const [params, setParams] = useSearchParams();
  const action = params.get("action") ?? "";
  const resourceType = params.get("resource") ?? "";
  const from = params.get("from") ?? "";
  const through = params.get("through") ?? "";
  function filter(values: Record<string, string>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) { if (value) next.set(key, value); else next.delete(key); }
    setParams(next, { replace: true });
  }
  const actionLabels: Record<string, string> = {
    "run.paused": t("auditTaskPaused"), "run.completed": t("auditTaskCompleted"),
    "secret.created": t("newSecret"), "secret.disabled": t("disableSecret"),
    "memory.shared_created": t("memorySharedCreatedAction"), "memory.shared_updated": t("memorySharedUpdatedAction"),
    "workspace.created": t("auditWorkspaceCreated"),
    "agent.created": t("newAgent"), "agent.draft_replaced": t("saveDraft"), "agent.published": t("publish"),
    "channel.binding_created": t("bindChannel"), "channel.binding_disabled": t("channelDisable"),
    "http_tool.registered": t("addHttpTool"), "mcp_server.registered": t("addMcpServer"),
    "service_account.created": t("newServiceAccount"),
    "subject.looked_up": t("subjectFind"), "subject.searched": t("subjectPartialFind"), "subject.erased": t("subjectErase"),
  };


  const query = new URLSearchParams(timeBounds(from, through));
  if (action.trim() !== "") query.set("action", action.trim());
  if (resourceType.trim() !== "") query.set("resource_type", resourceType.trim());
  const suffix = query.toString() === "" ? "" : `?${query.toString()}`;

  const events = useInfiniteQuery({
    queryKey: ["audit-events", workspaceId, suffix] as const,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api<AuditEventsPageResponse>(`/api/v1/audit-events${suffix}${suffix === "" ? "?" : "&"}offset=${pageParam}`, {
        workspace: workspaceId ?? "",
      }),
    enabled: workspaceId !== null,
    getNextPageParam: (lastPage, pages) => lastPage.has_more
      ? pages.reduce((count, page) => count + page.items.length, 0)
      : undefined,
  });

  const page = events.data?.pages[0];
  const items = [...new Map((events.data?.pages.flatMap((page) => page.items) ?? []).map((item) => [item.id, item])).values()];

  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  // The same `suffix` the table above is showing, on purpose: an export that
  // built its own query could answer a different question than the screen
  // the auditor clicked it from, and nothing about the file would say so.
  async function exportEvents(): Promise<void> {
    setExporting(true);
    setExportError(null);
    try {
      await download(`/api/v1/audit-events/export${suffix}`, {
        workspace: workspaceId ?? "",
      });
    } catch (caught) {
      setExportError(problemMessage(caught, t));
    } finally {
      setExporting(false);
    }
  }

  return (
    <Card>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>

        {page?.visibility === "redacted" ? (
          <Alert
            type="warning"
            showIcon
            message={t("auditRedacted")}
            description={t("auditRedactedHint")}
          />
        ) : null}
        {page?.visibility === "own_resources" ? (
          <Alert
            type="info"
            showIcon
            message={t("auditScopeOwn")}
            description={t("auditScopeOwnHint")}
          />
        ) : null}

        <Space wrap>
          <AutoComplete
            options={Object.entries(actionLabels).map(([value, label]) => ({ value, label }))}
            aria-label={t("auditFilterAction")}
            placeholder={t("auditFilterAction")}
            value={action}
            onChange={(value) => filter({ action: value })}
            allowClear
          />
          <AutoComplete
            options={[{ value: "run", label: t("runs") }, { value: "agent", label: t("agents") }, { value: "secret", label: t("secrets") }, { value: "memory", label: t("memoryReview") }]}
            aria-label={t("auditFilterResource")}
            placeholder={t("auditFilterResource")}
            value={resourceType}
            onChange={(value) => filter({ resource: value })}
            allowClear
          />
          <TimeRangeFilter from={from} through={through} onChange={(a, b) => filter({ from: a, through: b })} />
          <Button onClick={() => void exportEvents()} loading={exporting}>
            {t("auditExport")}
          </Button>
        </Space>
        {page?.visibility !== undefined && page.visibility !== "full" ? (
          // The file inherits the banner's narrowing, and a spreadsheet
          // carries no banner — so the page has to say it before the click,
          // not after.
          <Typography.Paragraph type="secondary">{t("auditExportScoped")}</Typography.Paragraph>
        ) : null}
        {exportError !== null ? (
          <Alert type="error" showIcon message={exportError} closable onClose={() => setExportError(null)} />
        ) : null}

        {events.isError ? (
          <Alert type="error" showIcon title={problemMessage(events.error, t)} action={
            <Button onClick={() => void (events.isFetchNextPageError ? events.fetchNextPage() : events.refetch())}>{t("retry")}</Button>
          } />
        ) : null}
        {items.length === 0 && !events.isLoading && !events.isError ? (
          <EmptyState title={t("auditEmpty")} />
        ) : (
          <Table
            rowKey="id"
            loading={events.isLoading}
            dataSource={items}
            pagination={false}
            scroll={{ x: 900 }}
            columns={[
              {
                title: t("auditWhen"),
                dataIndex: "created_at",
                render: (value: string) => moment(value, locale),
              },
              {
                title: t("auditActor"),
                dataIndex: "actor_type",
                render: (value: string, row) => (
                  <Space direction="vertical" size={0}>
                    <Tag>{value}</Tag>
                    <Typography.Text type="secondary" copyable={row.actor_id !== null}>
                      {row.actor_id ?? "—"}
                    </Typography.Text>
                  </Space>
                ),
              },
              { title: t("auditAction"), dataIndex: "action", render: (value: string) => <div>{actionLabels[value] && <div>{actionLabels[value]}</div>}<Typography.Text type="secondary">{value}</Typography.Text></div> },
              {
                title: t("auditResource"),
                dataIndex: "resource_type",
                render: (value: string, row) => (
                  <Space direction="vertical" size={0}>
                    <span>{value}</span>
                    {row.resource_id && ["run", "agent"].includes(value) ? <Link to={`/workspaces/${workspaceId}/${value === "run" ? "runs" : "agents"}/${row.resource_id}`}>{row.resource_id}</Link> : <Typography.Text type="secondary">{row.resource_id ?? "—"}</Typography.Text>}
                  </Space>
                ),
              },
              { title: t("auditResult"), dataIndex: "result" },
              {
                title: t("auditContext"),
                dataIndex: "context",
                render: (value: Record<string, unknown>) =>
                  Object.keys(value).length === 0 ? (
                    <Typography.Text type="secondary">—</Typography.Text>
                  ) : (
                    <Typography.Text code>{JSON.stringify(value)}</Typography.Text>
                  ),
              },
            ]}
          />
        )}
        {events.hasNextPage ? (
          <Button onClick={() => void events.fetchNextPage()} loading={events.isFetchingNextPage}>{t("auditMore")}</Button>
        ) : null}
      </Space>
    </Card>
  );
}
