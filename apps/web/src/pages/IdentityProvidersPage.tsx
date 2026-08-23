import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Modal, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { OidcProviderResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";

/**
 * The identity providers this deployment trusts.
 *
 * §21 makes "配置本地登录或 OIDC" the second step of the setup wizard. The
 * routes behind it shipped with the OIDC work — register, list, disable —
 * and nothing in either console referenced them, so the only way to add a
 * provider was an API call an administrator had to compose by hand. The
 * login page has been reading `/auth/oidc/available` the whole time and
 * would simply show no buttons.
 *
 * Instance-wide rather than per-workspace, like `ModelEndpointsPage`: the
 * route is platform-admin only and takes no workspace header.
 *
 * The client secret is **named, never typed**. `client_secret_ref` is an
 * environment variable name or a platform Secret id — the same two-shape
 * reference `ModelEndpointRow.credential_ref` uses — so no plaintext
 * reaches this page, its request bodies, or anything that logs them.
 */
export function IdentityProvidersPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<{
    issuer: string;
    clientId: string;
    clientSecretRef: string;
    discoveryUrl: string;
  }>();

  const listQuery = ["oidc-providers"] as const;
  const providers = useQuery({
    queryKey: listQuery,
    queryFn: () => api<OidcProviderResponse[]>("/api/v1/oidc/providers"),
  });

  const register = useMutation({
    mutationFn: (values: {
      issuer: string;
      clientId: string;
      clientSecretRef: string;
      discoveryUrl: string;
    }) =>
      api<OidcProviderResponse>("/api/v1/oidc/providers", {
        method: "POST",
        body: JSON.stringify({
          issuer: values.issuer,
          client_id: values.clientId,
          client_secret_ref: values.clientSecretRef,
          discovery_url: values.discoveryUrl,
          // The three OIDC itself defines for reading a person's identity.
          // Sent explicitly rather than left empty: an empty list means the
          // provider decides, and which claims arrive would then depend on
          // somebody else's default.
          scopes: ["openid", "email", "profile"],
        }),
      }),
    onSuccess: () => {
      setOpen(false);
      form.resetFields();
      void queryClient.invalidateQueries({ queryKey: listQuery });
    },
    onError: (caught) =>
      form.setFields([{ name: "issuer", errors: [problemMessage(caught, t)] }]),
  });

  const disable = useMutation({
    mutationFn: (providerId: string) =>
      api<OidcProviderResponse>(`/api/v1/oidc/providers/${providerId}/disable`, {
        method: "POST",
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: listQuery }),
  });

  if (providers.isError) {
    // Platform-admin only, so a refusal is an ordinary outcome here. An
    // empty table would say this deployment trusts nobody, which is a
    // different claim and one this reader cannot check.
    return (
      <Alert
        type="warning"
        showIcon
        message={problemMessage(providers.error, t)}
        description={t("identityProvidersForbiddenHint")}
      />
    );
  }

  const rows = providers.data ?? [];

  return (
    <>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("identityProviders")}</Typography.Title>
          <Typography.Paragraph type="secondary">
            {t("identityProvidersIntro")}
          </Typography.Paragraph>
        </div>
        <Button type="primary" onClick={() => setOpen(true)}>
          {t("registerIdentityProvider")}
        </Button>
      </div>

      <Card loading={providers.isPending} variant="borderless">
        {rows.length === 0 ? (
          <Empty description={t("identityProvidersEmpty")} />
        ) : (
          <Table<OidcProviderResponse>
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={rows}
            columns={[
              { title: t("oidcIssuer"), dataIndex: "issuer" },
              { title: t("oidcClientId"), dataIndex: "client_id" },
              {
                title: t("oidcSecretRef"),
                dataIndex: "client_secret_ref",
                // The name of the reference. There is no response field that
                // could hold the secret itself.
                render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
              },
              {
                title: t("oidcScopes"),
                dataIndex: "scopes",
                render: (value: string[]) => value.join(" "),
              },
              { title: t("oidcStatus"), dataIndex: "status", render: (v: string) => <Tag>{v}</Tag> },
              { title: t("oidcRegisteredAt"), dataIndex: "created_at", render: (v: string) => moment(v) },
              {
                title: "",
                key: "actions",
                render: (_value, row) =>
                  row.status === "enabled" ? (
                    <Button
                      danger
                      size="small"
                      loading={disable.isPending}
                      onClick={() => disable.mutate(row.id)}
                    >
                      {t("oidcDisable")}
                    </Button>
                  ) : null,
              },
            ]}
          />
        )}
      </Card>

      <Modal
        open={open}
        title={t("registerIdentityProvider")}
        okText={t("registerIdentityProviderConfirm")}
        cancelText={t("cancel")}
        confirmLoading={register.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => void form.submit()}
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary">{t("oidcSecretRefHint")}</Typography.Paragraph>
          <Form
            form={form}
            layout="vertical"
            requiredMark={false}
            onFinish={(values) => register.mutate(values)}
          >
            <Form.Item name="issuer" label={t("oidcIssuer")} rules={[{ required: true }]}>
              <Input placeholder="https://login.example.com" />
            </Form.Item>
            <Form.Item name="clientId" label={t("oidcClientId")} rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item
              name="clientSecretRef"
              label={t("oidcSecretRef")}
              rules={[{ required: true }]}
            >
              <Input placeholder="OIDC_CLIENT_SECRET" />
            </Form.Item>
            <Form.Item
              name="discoveryUrl"
              label={t("oidcDiscoveryUrl")}
              rules={[{ required: true }]}
            >
              <Input placeholder="https://login.example.com/.well-known/openid-configuration" />
            </Form.Item>
          </Form>
        </Space>
      </Modal>
    </>
  );
}
