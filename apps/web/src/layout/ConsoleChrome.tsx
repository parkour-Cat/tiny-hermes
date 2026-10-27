import { Alert, Avatar, Dropdown, Layout } from "antd";
import type { MenuProps } from "antd";
import type { ReactNode } from "react";
import { useEffect, useId, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useConsoleTheme } from "./ConsoleTheme";
import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { HermesMark } from "../ui/HermesMark";

export function BrandMark() {
  const { t } = useLocale();
  return (
    <Link to="/workspaces" className="th-brand">
      <HermesMark size={36} />
      <span className="th-word">{t("appName")}</span>
    </Link>
  );
}

/**
 * Shared chrome: one dark sider for everything, no top bar.
 *
 * The top bar held the person — language, theme, avatar, sign-out — and
 * cost every page 64 px above its content for things touched once a week.
 * They sit at the foot of the sider now, behind one menu, so the column that
 * says where you are also says who you are, and the content starts at the
 * top of the window.
 */
export function ConsoleChrome({ sidebar, children }: { sidebar: ReactNode; children: ReactNode }) {
  const auth = useAuth();
  const { t, locale, setLocale } = useLocale();
  const theme = useConsoleTheme();
  const navigate = useNavigate();
  const [actionError, setActionError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const bodyId = useId();
  const { pathname } = useLocation();

  // Choosing a page is what the menu was opened for; leaving it open would
  // keep the page under it.
  useEffect(() => setMenuOpen(false), [pathname]);

  async function logout(): Promise<void> {
    setActionError(null);
    try {
      await auth.logout();
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : t("requestFailed"));
    }
  }

  const items: MenuProps["items"] = [
    { key: "zh-CN", label: t("localeZh") },
    { key: "en-US", label: t("localeEn") },
    { type: "divider" },
    // Named for the theme it switches to, as the button it replaces was.
    { key: "theme", label: theme.dark ? t("themeLight") : t("themeDark") },
    { key: "workspaces", label: t("backToWorkspaces") },
    { type: "divider" },
    { key: "logout", label: t("logout") },
  ];

  function choose({ key }: { key: string }): void {
    if (key === "zh-CN" || key === "en-US") setLocale(key);
    else if (key === "theme") theme.toggle();
    else if (key === "workspaces") navigate("/workspaces");
    else if (key === "logout") void logout();
  }

  return (
    <Layout className="th-shell">
      <Layout.Sider className={`th-sider${menuOpen ? " is-open" : ""}`} width={248} theme="dark" trigger={null}>
        {/* Shown only on a narrow screen, where the sider stacks above the
            page: there everything but the logo folds behind it, or the
            navigation would take half the screen on every page. */}
        <button
          type="button"
          className="th-menu-toggle"
          aria-expanded={menuOpen}
          aria-controls={bodyId}
          onClick={() => setMenuOpen((open) => !open)}
        >
          {t("navMenu")}
        </button>
        <div className="th-sider-body" id={bodyId}>
        {sidebar}
        <div className="th-account">
          <Avatar className="th-account-avatar">{auth.user?.display_name.slice(0, 1).toUpperCase()}</Avatar>
          <div className="th-account-who">
            <span className="th-account-name">{auth.user?.display_name}</span>
            <span className="th-account-subject">{auth.user?.subject}</span>
          </div>
          <Dropdown
            trigger={["click"]}
            placement="topRight"
            menu={{ items, onClick: choose, selectedKeys: [locale] }}
          >
            <button type="button" className="th-account-menu" aria-label={t("accountMenu")}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <circle cx="5" cy="12" r="1" />
                <circle cx="12" cy="12" r="1" />
                <circle cx="19" cy="12" r="1" />
              </svg>
            </button>
          </Dropdown>
        </div>
        </div>
      </Layout.Sider>
      <Layout className="th-main">
        <Layout.Content className="th-content">
          {actionError === null ? null : (
            <Alert className="page-alert" type="error" title={actionError} showIcon />
          )}
          {children}
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
