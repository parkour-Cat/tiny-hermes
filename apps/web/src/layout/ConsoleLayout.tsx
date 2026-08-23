import { useQuery } from "@tanstack/react-query";
import { Alert, Avatar, Button, Layout, Select, Space, Typography } from "antd";
import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useConsoleTheme } from "./ConsoleTheme";

type WorkspaceSummary = {
  id: string;
  name: string;
  status: string;
};

const WORKSPACES_QUERY = ["workspaces"] as const;

export function ConsoleLayout() {
  const workspaceId = useWorkspaceId();
  const auth = useAuth();
  const { t, locale, setLocale } = useLocale();
  const theme = useConsoleTheme();
  const [actionError, setActionError] = useState<string | null>(null);
  // Only to put a name on the header. Membership is the server's answer, never
  // this list's: a Workspace missing from it still gets its requests sent and
  // its refusal shown, because a console that pre-filters is a console that can
  // disagree with the platform about who may see what.
  const workspaces = useQuery({
    queryKey: WORKSPACES_QUERY,
    queryFn: () => api<WorkspaceSummary[]>("/api/v1/workspaces"),
    enabled: workspaceId !== null,
  });

  if (workspaceId === null) {
    return (
      <main className="centered-state">
        <Alert
          type="error"
          title={t("invalidWorkspace")}
          description={t("invalidWorkspaceDetail")}
          action={
            <Link to="/workspaces">
              <Button>{t("backToWorkspaces")}</Button>
            </Link>
          }
          showIcon
        />
      </main>
    );
  }

  const current = (workspaces.data ?? []).find((workspace) => workspace.id === workspaceId);

  async function logout(): Promise<void> {
    setActionError(null);
    try {
      await auth.logout();
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : t("requestFailed"));
    }
  }

  return (
    <Layout className="app-layout">
      <Layout.Header className="app-header">
        <div className="console-identity">
          <Link to="/workspaces" className="header-brand">
            {t("appName")}
          </Link>
          <Typography.Text className="console-workspace" ellipsis>
            {current?.name ?? workspaceId}
          </Typography.Text>
          <nav className="console-nav">
            <NavLink to={`/workspaces/${workspaceId}/agents`}>{t("agents")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/runs`}>{t("runs")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/usage`}>{t("usage")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/members`}>{t("members")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/model-endpoints`}>{t("modelEndpoints")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/api-keys`}>{t("apiKeys")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/skills`}>{t("skills")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/skill-proposals`}>{t("proposals")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/approvals`}>{t("approvals")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/memory`}>{t("memoryReview")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/http-tools`}>{t("httpTools")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/mcp-servers`}>{t("mcpServers")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/outbound`}>{t("outboundScopes")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/secrets`}>{t("secrets")}</NavLink>
            <NavLink to={`/workspaces/${workspaceId}/audit`}>{t("audit")}</NavLink>
          </nav>
        </div>
        <Space wrap>
          <Select
            aria-label={t("language")}
            value={locale}
            onChange={(next) => setLocale(next)}
            options={[
              { value: "zh-CN", label: t("localeZh") },
              { value: "en-US", label: t("localeEn") },
            ]}
            popupMatchSelectWidth={false}
          />
          <Button onClick={() => theme.toggle()}>
            {theme.dark ? t("themeLight") : t("themeDark")}
          </Button>
          <Avatar>{auth.user?.display_name.slice(0, 1).toUpperCase()}</Avatar>
          <div className="user-summary">
            <Typography.Text>{auth.user?.display_name}</Typography.Text>
            <Typography.Text type="secondary">{auth.user?.subject}</Typography.Text>
          </div>
          <Button onClick={() => void logout()}>{t("logout")}</Button>
        </Space>
      </Layout.Header>
      <Layout.Content className="workspace-content">
        {actionError === null ? null : (
          <Alert className="page-alert" type="error" title={actionError} showIcon />
        )}
        <Outlet />
      </Layout.Content>
    </Layout>
  );
}
