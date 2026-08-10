import { Alert, Button, Card, Form, Input, Space, Typography } from "antd";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { t } from "../i18n/zh-CN";

type LoginValues = {
  subject: string;
  password: string;
};

export function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const initialized = Boolean((location.state as { initialized?: boolean } | null)?.initialized);

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
          <Link to="/bootstrap">{t("bootstrapLink")}</Link>
        </Space>
      </Card>
    </PublicShell>
  );
}

export function PublicShell({ children }: { children: React.ReactNode }) {
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
