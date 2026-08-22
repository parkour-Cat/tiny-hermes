import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Divider, Form, Input, Space, Typography } from "antd";
import { useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { OfferableProviderResponse } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";

type LoginValues = {
  subject: string;
  password: string;
};

export function LoginPage() {
  const t = useT();
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [params] = useSearchParams();
  const initialized = Boolean((location.state as { initialized?: boolean } | null)?.initialized);

  // Unauthenticated on purpose — see `available_providers`. A failure here
  // must not stop local login from rendering, so this deliberately has no
  // error branch: the section simply does not appear.
  const providers = useQuery({
    queryKey: ["oidc-available"] as const,
    queryFn: () => api<OfferableProviderResponse[]>("/api/v1/auth/oidc/available"),
  });
  const offered = providers.data ?? [];

  // A refused callback redirects back here. Without saying so, that is
  // indistinguishable from arriving at the login page normally, and the
  // person retries the same broken provider forever.
  const ssoFailed = params.get("sso_error") !== null;

  async function submit(values: LoginValues): Promise<void> {
    setSubmitting(true);
    setError(null);
    try {
      await auth.login(values);
      navigate("/workspaces", { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("loginFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <PublicShell>
      <Card className="auth-card" variant="borderless">
        <Space orientation="vertical" size="large" className="full-width">
          <div>
            <Typography.Title level={2}>{t("loginTitle")}</Typography.Title>
            <Typography.Text type="secondary">{t("appTagline")}</Typography.Text>
          </div>
          {initialized ? <Alert type="success" title={t("bootstrapSucceeded")} /> : null}
          {ssoFailed ? <Alert type="error" title={t("ssoFailed")} showIcon /> : null}
          {error === null ? null : <Alert type="error" title={error} showIcon />}
          <Form<LoginValues> layout="vertical" requiredMark={false} onFinish={submit}>
            <Form.Item
              name="subject"
              label={t("email")}
              rules={[
                { required: true, message: t("required") },
                { type: "email", message: t("invalidEmail") },
              ]}
            >
              <Input autoComplete="email" />
            </Form.Item>
            <Form.Item
              name="password"
              label={t("password")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={submitting} block>
              {t("login")}
            </Button>
          </Form>
          {offered.length === 0 ? null : (
            <>
              <Divider plain>{t("orUseSso")}</Divider>
              <Space orientation="vertical" size="small" className="full-width">
                {offered.map((provider) => (
                  // A real navigation, not a fetch: `/start` answers 302 to
                  // the identity provider, and XHR would follow that inside
                  // the page instead of handing the browser over.
                  <Button key={provider.id} block href={`/api/v1/auth/oidc/${provider.id}/start`}>
                    {t("signInWithOidc").replace("{issuer}", hostOf(provider.issuer))}
                  </Button>
                ))}
              </Space>
            </>
          )}
          <Link to="/bootstrap">{t("bootstrapLink")}</Link>
        </Space>
      </Card>
    </PublicShell>
  );
}

/** The issuer's host, which is what a person recognizes. The full URL is
 *  accurate and unreadable on a button; `accounts.google.com` is neither. */
function hostOf(issuer: string): string {
  try {
    return new URL(issuer).host;
  } catch {
    return issuer;
  }
}

export function PublicShell({ children }: { children: React.ReactNode }) {
  const t = useT();
  return (
    <main className="public-shell">
      <div className="brand-panel">
        <Typography.Text className="brand-kicker">{t("appName")}</Typography.Text>
        <Typography.Title>{t("appTagline")}</Typography.Title>
      </div>
      {children}
    </main>
  );
}
