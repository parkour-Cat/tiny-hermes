import { Alert, Avatar, Button, Layout, Select, Typography } from "antd";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { useConsoleTheme } from "./ConsoleTheme";

export function BrandMark() {
  const { t } = useLocale();
  return (
    <Link to="/workspaces" className="th-brand">
      <span className="th-mark" aria-hidden="true" />
      <span className="th-word">{t("appName")}</span>
    </Link>
  );
}

/**
 * Shared chrome: a sider for wayfinding, a thin top bar for the operator.
 *
 * Navigation used to share a single header row with the workspace name, the
 * locale switch, the theme, the avatar and sign-out. That row could not hold
 * six destinations without looking like a toolbar demo.
 */
export function ConsoleChrome({
  sidebar,
  children,
}: {
  sidebar: ReactNode;
  children: ReactNode;
}) {
  const auth = useAuth();
  const { t, locale, setLocale } = useLocale();
  const theme = useConsoleTheme();
  const [actionError, setActionError] = useState<string | null>(null);

  async function logout(): Promise<void> {
    setActionError(null);
    try {
      await auth.logout();
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : t("requestFailed"));
    }
  }

  return (
    <Layout className="th-shell">
      <Layout.Sider className="th-sider" width={232} theme="light" trigger={null}>
        {sidebar}
      </Layout.Sider>
      <Layout className="th-main">
        <Layout.Header className="th-topbar">
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
        </Layout.Header>
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

export function PageHeading({
  kicker,
  title,
  intro,
  extra,
}: {
  kicker?: string;
  title: string;
  intro?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        {kicker === undefined ? null : <p className="page-kicker">{kicker}</p>}
        <Typography.Title level={2}>{title}</Typography.Title>
        {intro === undefined ? null : (
          <Typography.Paragraph type="secondary">{intro}</Typography.Paragraph>
        )}
      </div>
      {extra}
    </div>
  );
}
