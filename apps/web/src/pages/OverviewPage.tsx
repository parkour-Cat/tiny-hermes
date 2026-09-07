import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Space, Typography } from "antd";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, RunResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { useInboxCount } from "../layout/useInboxCount";
import { PageHeading } from "../ui/PageHeading";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useWorkspacePermissions } from "../workspace/WorkspacePermissions";

export function OverviewPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const { writer } = useWorkspacePermissions();
  const base = `/workspaces/${workspaceId}`;
  const scope = { workspace: workspaceId ?? "" };
  const agents = useQuery({ queryKey: ["agents", workspaceId], queryFn: () => api<AgentResponse[]>("/api/v1/agents", scope), enabled: workspaceId !== null });
  const runs = useQuery({ queryKey: ["runs", workspaceId], queryFn: () => api<RunResponse[]>("/api/v1/runs", scope), enabled: workspaceId !== null });
  const inbox = useInboxCount();
  return <>
    <PageHeading title={t("overview")} intro={t("overviewIntro")} />
    <div className="overview-grid">
      <Card title={t("overviewPublished")} loading={agents.isPending}>
        {agents.isError ? <Alert type="error" title={problemMessage(agents.error, t)} action={<Button onClick={() => void agents.refetch()}>{t("retry")}</Button>} /> : <Typography.Paragraph>{agents.data?.filter((agent) => agent.current_version_id !== null).length} / {agents.data?.length}</Typography.Paragraph>}
        <Link to={`${base}/agents`}>{t("agents")}</Link>
      </Card>
      <Card title={t("overviewTasks")} loading={runs.isPending}>
        {runs.isError ? <Alert type="error" title={problemMessage(runs.error, t)} action={<Button onClick={() => void runs.refetch()}>{t("retry")}</Button>} /> : <Typography.Paragraph>{runs.data?.length}</Typography.Paragraph>}
        <Link to={`${base}/runs`}>{t("runs")}</Link>
      </Card>
      <Card title={t("inboxUnified")}><Typography.Paragraph>{inbox === null ? t("loading") : inbox === "?" ? t("inboxCountUnknown") : inbox}</Typography.Paragraph><Link to={`${base}/inbox`}>{t("navInbox")}</Link></Card>
    </div>
    {writer && <Card className="page-alert" title={t("overviewNext")}><Space direction="vertical">
      <Link to={`${base}/agents`}>{t("overviewAgentStep")}</Link>
      <Link to={`${base}/runs`}>{t("overviewRunStep")}</Link>
      <Link to={`${base}/channels`}>{t("overviewChannelStep")}</Link>
    </Space></Card>}
  </>;
}
