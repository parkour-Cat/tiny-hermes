import { useQuery } from "@tanstack/react-query";
import { Alert, Button } from "antd";
import { Link, NavLink, Outlet } from "react-router-dom";

import { api } from "../api/client";
import { useLocale } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { BrandMark, ConsoleChrome } from "./ConsoleChrome";

type WorkspaceSummary = {
  id: string;
  name: string;
  status: string;
};

const WORKSPACES_QUERY = ["workspaces"] as const;

const NAV: { to: string; label: MessageKey }[] = [
  { to: "agents", label: "agents" },
  { to: "runs", label: "runs" },
  { to: "members", label: "members" },
  { to: "model-endpoints", label: "modelEndpoints" },
  { to: "api-keys", label: "apiKeys" },
  { to: "secrets", label: "secrets" },
];

export function ConsoleLayout() {
  const workspaceId = useWorkspaceId();
  const { t } = useLocale();
  // Only to put a name on the sider. Membership is the server's answer, never
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

  return (
    <ConsoleChrome
      sidebar={
        <>
          <BrandMark />
          <div className="th-workspace-chip" title={workspaceId}>
            {current?.name ?? t("workspaceTitle")}
          </div>
          <nav className="th-nav">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={`/workspaces/${workspaceId}/${item.to}`}
                className={({ isActive }) => (isActive ? "th-nav-link active" : "th-nav-link")}
              >
                {t(item.label)}
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
