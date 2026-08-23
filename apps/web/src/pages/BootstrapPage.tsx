import { Alert, Button, Card, Form, Input, Space, Typography } from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useT } from "../i18n/locale";
import type { User } from "../auth/AuthProvider";
import { PublicShell } from "./LoginPage";

type BootstrapValues = {
  bootstrapToken: string;
  subject: string;
  displayName: string;
  password: string;
};

export function BootstrapPage() {
  const t = useT();
  const navigate = useNavigate();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(values: BootstrapValues): Promise<void> {
    setSubmitting(true);
    setError(null);
    try {
      await api<User>("/api/v1/bootstrap", {
        method: "POST",
        headers: { "X-Bootstrap-Token": values.bootstrapToken },
        body: JSON.stringify({
          subject: values.subject,
          display_name: values.displayName,
          password: values.password,
        }),
      });
      navigate("/login", { replace: true, state: { initialized: true } });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("requestFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <PublicShell>
      <Card className="auth-card" variant="borderless">
        <Space orientation="vertical" size="large" className="full-width">
          <div>
            <Typography.Title level={2}>{t("bootstrapTitle")}</Typography.Title>
            <Typography.Text type="secondary">{t("bootstrapHint")}</Typography.Text>
          </div>
          {error === null ? null : <Alert type="error" title={error} showIcon />}
          <Form<BootstrapValues> layout="vertical" requiredMark={false} onFinish={submit}>
            <Form.Item
              name="bootstrapToken"
              label={t("bootstrapToken")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input.Password autoComplete="off" />
            </Form.Item>
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
              name="displayName"
              label={t("displayName")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input autoComplete="name" />
            </Form.Item>
            <Form.Item
              name="password"
              label={t("password")}
              rules={[
                { required: true, message: t("required") },
                { min: 12, message: t("passwordMinimum") },
              ]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={submitting} block>
              {t("bootstrapSubmit")}
            </Button>
          </Form>
          <Link to="/login">{t("backToLogin")}</Link>
        </Space>
      </Card>
    </PublicShell>
  );
}
