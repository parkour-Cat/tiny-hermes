import { useQuery } from "@tanstack/react-query";
import { Alert, Badge, Button } from "antd";
import { Link, NavLink, Outlet } from "react-router-dom";

import { BrandMark, ConsoleChrome } from "./ConsoleChrome";
import { NAV_GROUPS } from "./navigation";
import { useInboxCount } from "./useInboxCount";
import { api } from "../api/client";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type WorkspaceSummary = {
  id: string;
  name: string;
  status: string;
};

const WORKSPACES_QUERY = ["workspaces"] as const;

export function ConsoleLayout() {
  const workspaceId = useWorkspaceId();
  const t = useT();
  // Only to put a name on the sider. Membership is the server's answer, never
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

  return (
    <ConsoleChrome
      sidebar={
        <>
          <BrandMark />
          <div className="th-workspace-chip">{current?.name ?? workspaceId}</div>
          <nav className="th-nav console-nav" aria-label={t("workspaceTitle")}>
            {/* 只有一段的入口（Agents、运行、渠道）直接指向那一段的路径，不经过
                合并页——给一个只有一段的页面套一层分段外壳，只会多一层没有内容
                的标题。 */}
            {NAV_GROUPS.map((group) => (
              <NavLink
                key={group.key}
                className="th-nav-link"
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
          <Link to="/workspaces" className="th-nav-foot">
            {t("backToWorkspaces")}
          </Link>
        </>
      }
    >
      <Outlet />
    </ConsoleChrome>
  );
}
