import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Select, Space, Tag, Typography , Modal } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { RewrapResponse, SecretResponse } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type CreateValues = {
  name: string;
  scope: "workspace" | "platform";
  plaintext: string;
};

export function SecretsPage() {
  const t = useT();
  const [modal, contextHolder] = Modal.useModal();
  const auth = useAuth();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<CreateValues>();
  const [error, setError] = useState<string | null>(null);
  const [rewrapNote, setRewrapNote] = useState<string | null>(null);
  const admin = auth.user?.is_platform_admin === true;
  const scope = { workspace: workspaceId ?? "" };
  const listQuery = ["secrets", workspaceId] as const;

  const listed = useQuery({
    queryKey: listQuery,
    queryFn: () => api<SecretResponse[]>("/api/v1/secrets", scope),
    enabled: workspaceId !== null,
  });

  const create = useMutation({
    mutationFn: (values: CreateValues) =>
      api<SecretResponse>("/api/v1/secrets", {
        ...scope,
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (created) => {
      queryClient.setQueryData<SecretResponse[]>(listQuery, (current = []) => [
        ...current,
        created,
      ]);
      form.resetFields();
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const disable = useMutation({
    mutationFn: (secretId: string) =>
      api<SecretResponse>(`/api/v1/secrets/${secretId}/disable`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData<SecretResponse[]>(listQuery, (current = []) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const rewrap = useMutation({
    mutationFn: () =>
      api<RewrapResponse>("/api/v1/secrets/rewrap", { ...scope, method: "POST" }),
    onSuccess: (result) => {
      setRewrapNote(
        `${t("rewrapProcessed")}${String(result.processed)}${t("rewrapRemaining")}${String(result.remaining)}`,
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  if (listed.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(listed.error, t)}
        action={<Button onClick={() => void listed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("secretsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("secretsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {rewrapNote === null ? null : (
        <Alert className="page-alert" type="success" title={rewrapNote} showIcon />
      )}
      <Card title={t("newSecret")} variant="borderless" className="page-alert">
        <Form<CreateValues>
          form={form}
          layout="inline"
          requiredMark={false}
          initialValues={{ scope: "workspace" }}
          onFinish={(values) => create.mutate(values)}
        >
          <Form.Item
            name="name"
            label={t("secretName")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="scope" label={t("secretScope")} rules={[{ required: true }]}>
            <Select
              options={[
                { value: "workspace", label: t("secretScopeWorkspace") },
                ...(admin ? [{ value: "platform" as const, label: t("secretScopePlatform") }] : []),
              ]}
            />
          </Form.Item>
          <Form.Item
            name="plaintext"
            label={t("secretPlaintext")}
            rules={[{ required: true, message: t("required") }]}
          >
            <Input.Password autoComplete="off" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={create.isPending}>
              {t("create")}
            </Button>
          </Form.Item>
        </Form>
      </Card>
      {admin ? (
        <Card variant="borderless" className="page-alert">
          <Button
            loading={rewrap.isPending}
            onClick={() =>
                            void modal.confirm({
                              title: t("rewrapSecrets"),
                              content: t("rewrapWarning"),
                              okText: t("confirm"),
                              cancelText: t("cancel"),
                              onOk: () => rewrap.mutateAsync().catch(() => undefined),
                            })
                          }
          >
            {t("rewrapSecrets")}
          </Button>
        </Card>
      ) : null}
      <Card loading={listed.isPending} variant="borderless">
        {(listed.data ?? []).length === 0 ? (
          <Empty description={t("emptySecrets")} />
        ) : (
          (listed.data ?? []).map((secret) => (
            <article key={secret.id} className="workspace-row">
              <div className="workspace-summary">
                <Typography.Title level={4}>{secret.name}</Typography.Title>
                <Space wrap>
                  <Tag>{secret.scope}</Tag>
                  <Tag>{secret.status}</Tag>
                  <Typography.Text code>{secret.mask}</Typography.Text>
                  {secret.status === "active" ? (
                    <Button
                      loading={disable.isPending}
                      onClick={() =>
                            void modal.confirm({
                              title: t("disableSecret"),
                              content: t("disableSecretWarning"),
                              okText: t("confirm"),
                              cancelText: t("cancel"),
                              onOk: () => disable.mutateAsync(secret.id).catch(() => undefined),
                            })
                          }
                    >
                      {t("disableSecret")}
                    </Button>
                  ) : null}
                </Space>
              </div>
            </article>
          ))
        )}
      </Card>
    </>
  );
}
