import { Alert, Avatar, Button, Layout, Select, Typography } from "antd";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { BrandMark } from "./ConsoleChrome";
import { useConsoleTheme } from "./ConsoleTheme";

export function ChatChrome({
  title,
  children,
}: {
  title?: string;
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
    <Layout className="th-shell th-chat-shell">
      <Layout.Header className="th-topbar th-chat-topbar">
        <BrandMark />
        {title === undefined ? null : <Typography.Text className="th-chat-title">{title}</Typography.Text>}
        <span className="th-chat-spacer" />
        <Link to="/chat" className="th-chat-link">
          {t("switchAgent")}
        </Link>
        <Link to="/workspaces" className="th-chat-link">
          {t("openConsole")}
        </Link>
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
        <Button onClick={() => theme.toggle()}>{theme.dark ? t("themeLight") : t("themeDark")}</Button>
        <Avatar>{auth.user?.display_name.slice(0, 1).toUpperCase()}</Avatar>
        <div className="user-summary">
          <Typography.Text>{auth.user?.display_name}</Typography.Text>
          <Typography.Text type="secondary">{auth.user?.subject}</Typography.Text>
        </div>
        <Button onClick={() => void logout()}>{t("logout")}</Button>
      </Layout.Header>
      <Layout.Content className="th-content th-chat-content">
        {actionError === null ? null : (
          <Alert className="page-alert" type="error" title={actionError} showIcon />
        )}
        {children}
      </Layout.Content>
    </Layout>
  );
}
