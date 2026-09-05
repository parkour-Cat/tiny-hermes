import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Space, Table, Tag, Typography } from "antd";
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
  const [action, setAction] = useState("");
  const [resourceType, setResourceType] = useState("");

  const query = new URLSearchParams();
  if (action.trim() !== "") query.set("action", action.trim());
  if (resourceType.trim() !== "") query.set("resource_type", resourceType.trim());
  const suffix = query.toString() === "" ? "" : `?${query.toString()}`;

  const events = useQuery({
    queryKey: ["audit-events", workspaceId, action, resourceType] as const,
    queryFn: () =>
      api<AuditEventsPageResponse>(`/api/v1/audit-events${suffix}`, {
        workspace: workspaceId ?? "",
      }),
    enabled: workspaceId !== null,
  });

  const page = events.data;
  const items = page?.items ?? [];

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
    <Card title={t("audit")}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Paragraph type="secondary">{t("auditIntro")}</Typography.Paragraph>

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
          <Input
            aria-label={t("auditFilterAction")}
            placeholder={t("auditFilterAction")}
            value={action}
            onChange={(event) => setAction(event.target.value)}
            allowClear
          />
          <Input
            aria-label={t("auditFilterResource")}
            placeholder={t("auditFilterResource")}
            value={resourceType}
            onChange={(event) => setResourceType(event.target.value)}
            allowClear
          />
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

        {items.length === 0 && !events.isLoading ? (
          <EmptyState title={t("auditEmpty")} />
        ) : (
          <Table
            rowKey="id"
            loading={events.isLoading}
            dataSource={items}
            pagination={false}
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
              { title: t("auditAction"), dataIndex: "action" },
              {
                title: t("auditResource"),
                dataIndex: "resource_type",
                render: (value: string, row) => (
                  <Space direction="vertical" size={0}>
                    <span>{value}</span>
                    <Typography.Text type="secondary">{row.resource_id ?? "—"}</Typography.Text>
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
      </Space>
    </Card>
  );
}
