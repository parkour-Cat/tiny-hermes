import { useInfiniteQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Radio, Select, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { ApprovalsPageResponse, MemoryResponse, ProposalResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { moment } from "../i18n/moment";
import { useProposalAccess } from "../workspace/useProposalAccess";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type Kind = "approvals" | "proposals" | "memory";
type Row = { id: string; kind: Kind; title: string; detail: string; time: string; status: string };
type Page = { items: Row[]; more: boolean; next: number };
const kinds: Kind[] = ["approvals", "proposals", "memory"];

export function InboxQueue() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const access = useProposalAccess();
  const [view, setView] = useState("actionable");
  const [filter, setFilter] = useState("all");
  const labels = { approvals: t("approvals"), proposals: t("proposals"), memory: t("memoryReview") };
  const scope = { workspace: workspaceId ?? "" };

  async function page(kind: Kind, offset: number): Promise<Page> {
    if (kind === "approvals") {
      const query = new URLSearchParams({ limit: "20", offset: String(offset) });
      if (view === "history") for (const status of ["approved", "rejected", "expired"]) query.append("status", status);
      else if (view === "actionable") query.set("approval_type", "governance_approval");
      else if (access.admin) query.set("approval_type", "user_confirmation");
      const result = await api<ApprovalsPageResponse>(`/api/v1/approvals?${query}`, scope);
      return { items: result.items.map((row) => ({ id: row.id, kind, title: row.tool, detail: typeof row.document.target === "string" ? row.document.target : t("approvalTargetUnspecified"), time: row.decided_at ?? row.expires_at, status: row.status })), more: result.has_more, next: offset + 20 };
    }
    if (kind === "proposals") {
      const statuses = view === "history" ? ["approved", "rejected"] : ["pending"];
      const results = (await Promise.all(statuses.map((status) => api<ProposalResponse[]>(`/api/v1/skill-proposals?status=${status}`, scope)))).flat();
      const visible = results.filter((row) => view === "history" || (view === "actionable" ? access.canDecide(row) : !access.canDecide(row)));
      return { items: visible.map((row) => ({ id: row.id, kind, title: row.name, detail: row.description, time: row.decided_at ?? row.created_at, status: row.status })), more: false, next: 0 };
    }
    if (view !== "history") {
      const rows = await api<MemoryResponse[]>("/api/v1/memories/pending", scope);
      return { items: rows.map((row) => ({ id: row.id, kind, title: row.body, detail: row.kind, time: row.created_at, status: row.status })), more: false, next: 0 };
    }
    const result = await api<{ items: MemoryResponse[]; has_more: boolean }>(`/api/v1/memories?limit=20&offset=${offset}`, scope);
    return { items: result.items.filter((row) => row.status !== "pending").map((row) => ({ id: row.id, kind, title: row.body, detail: row.kind, time: row.updated_at, status: row.status })), more: result.has_more, next: offset + 20 };
  }

  function useQueue(kind: Kind, allowed: boolean) {
    return useInfiniteQuery({
      queryKey: ["inbox-count", workspaceId, "queue", view, kind], initialPageParam: 0,
      queryFn: ({ pageParam }) => page(kind, pageParam),
      getNextPageParam: (last: Page) => last.more ? last.next : undefined,
      enabled: workspaceId !== null && !access.loading && allowed,
    });
  }
  const approvals = useQueue("approvals", view !== "actionable" || access.admin);
  const proposals = useQueue("proposals", view !== "actionable" || access.writer);
  const memory = useQueue("memory", access.admin && view !== "waiting");
  const enabled = [view !== "actionable" || access.admin, view !== "actionable" || access.writer, access.admin && view !== "waiting"];
  const queries = [approvals, proposals, memory];
  const active = queries.filter((_, i) => enabled[i] && (filter === "all" || filter === kinds[i]));
  const rows = active.flatMap((query) => query.data?.pages.flatMap((result) => result.items) ?? [])
    .sort((a, b) => b.time.localeCompare(a.time) || a.id.localeCompare(b.id));
  return <>
    <Space wrap className="page-alert"><Radio.Group value={view} onChange={(event) => setView(String(event.target.value))} options={[
      { value: "actionable", label: t("approvalMyQueue") }, { value: "waiting", label: t("approvalOthersQueue") }, { value: "history", label: t("approvalHistoryQueue") },
    ]} /><Select aria-label={t("inboxKind")} value={filter} onChange={setFilter} options={[{ value: "all", label: t("inboxAllKinds") }, ...kinds.map((kind) => ({ value: kind, label: labels[kind] }))]} /></Space>
    {access.unknown && <Alert type="warning" title={t("inboxCountUnknown")} />}
    {active.filter((query) => query.isError).map((query, i) => <Alert key={i} type="error" title={problemMessage(query.error, t)} action={<Button onClick={() => void query.refetch()}>{t("retry")}</Button>} />)}
    <Card loading={access.loading || active.some((query) => query.isPending)} variant="borderless">
      {rows.length === 0 && !access.unknown && !active.some((query) => query.isError) ? <Typography.Paragraph>{t("inboxNoItems")}</Typography.Paragraph> : null}
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>{rows.map((row) => <article key={`${row.kind}:${row.id}`} className="workspace-row">
        <div className="workspace-summary"><Tag>{labels[row.kind]}</Tag><Typography.Text type="secondary">{row.kind === "approvals" && row.status === "pending" ? `${t("approvalExpires")} · ` : ""}{moment(row.time)}</Typography.Text><Typography.Paragraph strong>{row.title}</Typography.Paragraph><Typography.Paragraph type="secondary">{row.detail}</Typography.Paragraph>
          <Link to={`?item=${encodeURIComponent(row.id)}#${row.kind}`}>{t(view === "actionable" ? "inboxOpen" : "inboxView")}</Link>
        </div>
      </article>)}</Space>
    </Card>
    <Space wrap className="page-alert">{queries.map((query, index) => enabled[index] && query.hasNextPage ? <Button key={kinds[index]} loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>{t("auditMore")} · {labels[kinds[index]!]}</Button> : null)}</Space>
  </>;
}
