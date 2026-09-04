import { useQuery } from "@tanstack/react-query";
import { Alert, Avatar, Badge, Button, Layout, Select, Space, Typography } from "antd";
import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useConsoleTheme } from "./ConsoleTheme";
import { NAV_GROUPS } from "./navigation";
import { useInboxCount } from "./useInboxCount";

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
  const inboxCount = useInboxCount();
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
        {/* Two rows. One row could not hold a brand, a workspace name,
            eighteen destinations and the account controls without every one
            of them shrinking below its own text: the brand came out as
            "TINY-" above "HERMES" and the workspace name as "fe…". The nav
            takes the second row alone because it is the part that grows. */}
        <div className="header-top">
          <div className="console-identity">
            <Link to="/workspaces" className="header-brand">
              {t("appName")}
            </Link>
            <Typography.Text className="console-workspace" ellipsis>
              {current?.name ?? workspaceId}
            </Typography.Text>
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
        </div>
        <nav className="console-nav">
          {/* 只有一段的入口（Agents、运行、渠道）直接指向那一段的路径，不经过
              合并页——给一个只有一段的页面套一层分段外壳，只会多一层没有内容
              的标题。 */}
          {NAV_GROUPS.map((group) => (
            <NavLink
              key={group.key}
              to={`/workspaces/${workspaceId}/${
                group.sections.length === 1 ? group.sections[0]!.path : group.key
              }`}
              title={t(group.introKey)}
            >
              {t(group.labelKey)}
              {group.key === "inbox" && inboxCount !== null ? (
                <Badge count={inboxCount} className="nav-badge" />
              ) : null}
            </NavLink>
          ))}
        </nav>
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
