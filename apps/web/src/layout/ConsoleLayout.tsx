import { useQuery } from "@tanstack/react-query";
import { Alert, Badge, Button, Select } from "antd";
import { Link, NavLink, Outlet, useNavigate, useLocation } from "react-router-dom";

import { BrandMark, ConsoleChrome } from "./ConsoleChrome";
import { NAV_GROUPS, visibleSections } from "./navigation";
import { useInboxCount } from "./useInboxCount";
import { api } from "../api/client";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useMyRole } from "../workspace/useMyRole";
import { useAuth } from "../auth/AuthProvider";
import { WorkspacePermissions } from "../workspace/WorkspacePermissions";

type WorkspaceSummary = {
  id: string;
  name: string;
  status: string;
};

const WORKSPACES_QUERY = ["workspaces"] as const;

export function ConsoleLayout() {
  const workspaceId = useWorkspaceId();
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const { role } = useMyRole();
  const { user } = useAuth();
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
          <Select className="th-workspace-chip" aria-label={t("switchWorkspace")} value={workspaceId}
            loading={workspaces.isPending} style={{ width: "100%" }}
            options={(workspaces.data ?? (current ? [current] : [{ id: workspaceId, name: workspaceId }])).map((entry) => ({ value: entry.id, label: entry.name }))}
            onChange={(id: string) => {
              const section = location.pathname.split("/")[3] ?? "agents";
              navigate(`/workspaces/${id}/${section}${location.hash}`);
            }} />
          <nav className="th-nav console-nav" aria-label={t("workspaceTitle")}>
            {/* 只有一段的入口（Agents、运行、渠道）直接指向那一段的路径，不经过
                合并页——给一个只有一段的页面套一层分段外壳，只会多一层没有内容
                的标题。 */}
            {NAV_GROUPS.filter((group) => role !== null && visibleSections(group, role, user?.is_platform_admin === true).length > 0).map((group) => (
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
                  <Badge count={inboxCount} title={t(inboxCount === "?" ? "inboxCountUnknown" : "inboxCountHint")} className="nav-badge" />
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
      <WorkspacePermissions role={role} platform={user?.is_platform_admin === true}><div key={workspaceId}><Outlet /></div></WorkspacePermissions>
    </ConsoleChrome>
  );
}
