import { useQuery } from "@tanstack/react-query";
import { Alert, Badge, Button, Select } from "antd";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate, useLocation } from "react-router-dom";

import { BrandMark, ConsoleChrome } from "./ConsoleChrome";
import { CommandPalette } from "./CommandPalette";
import { NAV_CATEGORIES, NAV_GROUPS, visibleSections, type NavGroup } from "./navigation";
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
  const { role, missing } = useMyRole();
  const { user } = useAuth();
  // Only to put a name on the sider. Membership is the server's answer, never
  // this list's: a Workspace missing from it still gets its requests sent and
  // its refusal shown, because a console that pre-filters is a console that can
  // disagree with the platform about who may see what.
  const inboxCount = useInboxCount();
  const [searching, setSearching] = useState(false);
  useEffect(() => {
    function onKey(event: KeyboardEvent): void {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearching(true);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const workspaces = useQuery({
    queryKey: WORKSPACES_QUERY,
    queryFn: () => api<WorkspaceSummary[]>("/api/v1/workspaces"),
    enabled: workspaceId !== null,
  });

  if (workspaceId === null || missing) {
    return (
      <main className="centered-state">
        <Alert
          type="error"
          title={t(missing ? "workspaceNotFound" : "invalidWorkspace")}
          description={t(missing ? "workspaceNotFoundDetail" : "invalidWorkspaceDetail")}
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
          <button type="button" className="th-jump" onClick={() => setSearching(true)}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="M20 20l-3.5-3.5" />
            </svg>
            <span className="th-jump-label">{t("commandPalette")}</span>
            <kbd aria-hidden="true">⌘K</kbd>
          </button>
          <CommandPalette
            workspaceId={workspaceId}
            role={role}
            isPlatformAdmin={user?.is_platform_admin === true}
            open={searching}
            onClose={() => setSearching(false)}
          />
          <nav className="th-nav console-nav" aria-label={t("workspaceTitle")}>
            {NAV_CATEGORIES.map((category) => {
              const groups = category.groups
                .map((key) => NAV_GROUPS.find((group) => group.key === key))
                .filter((group): group is NavGroup =>
                  group !== undefined && role !== null &&
                  visibleSections(group, role, user?.is_platform_admin === true).length > 0);
              if (groups.length === 0) return null;
              const label = category.labelKey === null ? null : t(category.labelKey);
              return (
                <div key={category.key} className="th-nav-group" role="group" aria-label={label ?? undefined}>
                  {label === null ? null : <div className="th-nav-label" aria-hidden="true">{label}</div>}
                  {/* 只有一段的入口（Agents、运行、渠道）直接指向那一段的路径，不经过
                      合并页——给一个只有一段的页面套一层分段外壳，只会多一层没有内容
                      的标题。 */}
                  {groups.map((group) => (
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
                </div>
              );
            })}
          </nav>
        </>
      }
    >
      <WorkspacePermissions role={role} platform={user?.is_platform_admin === true}><div key={workspaceId}><Outlet /></div></WorkspacePermissions>
    </ConsoleChrome>
  );
}
