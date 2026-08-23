import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Checkbox, Empty, Form, Input, Select, Space, Tag, Typography , Modal } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  ApiKeyResponse,
  IssuedApiKeyResponse,
  ServiceAccountResponse,
} from "../api/types";
import { API_KEY_SCOPES, VIEWER_API_KEY_SCOPES } from "../api/types";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type AccountValues = {
  name: string;
  role: "developer" | "viewer";
};

type KeyValues = {
  scopes: string[];
};

export function ApiKeysPage() {
  const t = useT();
  const [modal, contextHolder] = Modal.useModal();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [accountForm] = Form.useForm<AccountValues>();
  const [error, setError] = useState<string | null>(null);
  const [plaintext, setPlaintext] = useState<IssuedApiKeyResponse | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const accountsQuery = ["service-accounts", workspaceId] as const;

  const accounts = useQuery({
    queryKey: accountsQuery,
    queryFn: () => api<ServiceAccountResponse[]>("/api/v1/service-accounts", scope),
    enabled: workspaceId !== null,
  });

  const keyQueries = useQueries({
    queries: (accounts.data ?? []).map((account) => ({
      queryKey: ["api-keys", workspaceId, account.id] as const,
      queryFn: () =>
        api<ApiKeyResponse[]>(`/api/v1/service-accounts/${account.id}/api-keys`, scope),
      enabled: workspaceId !== null,
    })),
  });

  const createAccount = useMutation({
    mutationFn: (values: AccountValues) =>
      api<ServiceAccountResponse>("/api/v1/service-accounts", {
        ...scope,
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (created) => {
      queryClient.setQueryData<ServiceAccountResponse[]>(accountsQuery, (current = []) => [
        ...current,
        created,
      ]);
      accountForm.resetFields();
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const disableAccount = useMutation({
    mutationFn: (accountId: string) =>
      api<ServiceAccountResponse>(`/api/v1/service-accounts/${accountId}/disable`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData<ServiceAccountResponse[]>(accountsQuery, (current = []) =>
        current.map((account) => (account.id === updated.id ? updated : account)),
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const createKey = useMutation({
    mutationFn: ({ accountId, scopes }: { accountId: string; scopes: string[] }) =>
      api<IssuedApiKeyResponse>(`/api/v1/service-accounts/${accountId}/api-keys`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ scopes }),
      }),
    onSuccess: (issued) => {
      queryClient.setQueryData<ApiKeyResponse[]>(
        ["api-keys", workspaceId, issued.service_account_id],
        (current = []) => [...current, issued],
      );
      setPlaintext(issued);
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const revoke = useMutation({
    mutationFn: (keyId: string) =>
      api<ApiKeyResponse>(`/api/v1/api-keys/${keyId}/revoke`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData<ApiKeyResponse[]>(
        ["api-keys", workspaceId, updated.service_account_id],
        (current = []) => current.map((key) => (key.id === updated.id ? updated : key)),
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  if (accounts.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(accounts.error, t)}
        action={<Button onClick={() => void accounts.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("serviceAccountsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("serviceAccountsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {plaintext === null ? null : (
        <Alert
          className="page-alert"
          type="success"
          title={t("keyPlaintextOnce")}
          description={
            <Space direction="vertical">
              <Typography.Text code copyable>
                {plaintext.token}
              </Typography.Text>
              <Button onClick={() => setPlaintext(null)}>{t("dismissPlaintext")}</Button>
            </Space>
          }
          showIcon
        />
      )}
      <Card title={t("newServiceAccount")} variant="borderless" className="page-alert">
        <Form<AccountValues>
          form={accountForm}
          layout="inline"
          requiredMark={false}
          initialValues={{ role: "developer" }}
          onFinish={(values) => createAccount.mutate(values)}
        >
          <Form.Item
            name="name"
            label={t("accountName")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="role" label={t("accountRole")} rules={[{ required: true }]}>
            <Select
              options={[
                { value: "developer", label: t("memberRoleDeveloper") },
                { value: "viewer", label: t("memberRoleViewer") },
              ]}
            />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={createAccount.isPending}>
              {t("create")}
            </Button>
          </Form.Item>
        </Form>
      </Card>
      <Card loading={accounts.isPending} variant="borderless">
        {(accounts.data ?? []).length === 0 ? (
          <Empty description={t("emptyServiceAccounts")} />
        ) : (
          (accounts.data ?? []).map((account, index) => {
            const keys = keyQueries[index]?.data ?? [];
            const allowed = account.role === "viewer" ? VIEWER_API_KEY_SCOPES : API_KEY_SCOPES;
            return (
              <article key={account.id} className="workspace-row">
                <div className="workspace-summary">
                  <Typography.Title level={4}>{account.name}</Typography.Title>
                  <Space wrap>
                    <Tag>{account.role}</Tag>
                    <Tag>{account.status}</Tag>
                    {account.status === "active" ? (
                      <Button
                        loading={disableAccount.isPending}
                        onClick={() =>
                            void modal.confirm({
                              title: t("disableAccount"),
                              content: t("disableAccountWarning"),
                              okText: t("confirm"),
                              cancelText: t("cancel"),
                              onOk: () => disableAccount.mutateAsync(account.id).catch(() => undefined),
                            })
                          }
                      >
                        {t("disableAccount")}
                      </Button>
                    ) : null}
                  </Space>
                  {keys.length === 0 ? (
                    <Typography.Paragraph type="secondary">{t("emptyApiKeys")}</Typography.Paragraph>
                  ) : (
                    keys.map((key) => (
                      <Space key={key.id} wrap>
                        <Typography.Text code>{key.prefix}</Typography.Text>
                        <Typography.Text type="secondary">{key.scopes.join(", ")}</Typography.Text>
                        {key.revoked_at === null ? (
                          <Button
                            onClick={() =>
                            void modal.confirm({
                              title: t("revokeKey"),
                              content: t("revokeKeyWarning"),
                              okText: t("confirm"),
                              cancelText: t("cancel"),
                              onOk: () => revoke.mutateAsync(key.id).catch(() => undefined),
                            })
                          }
                          >
                            {t("revokeKey")}
                          </Button>
                        ) : (
                          <Tag>{key.revoked_at}</Tag>
                        )}
                      </Space>
                    ))
                  )}
                  {account.status === "active" ? (
                    <Form<KeyValues>
                      layout="inline"
                      requiredMark={false}
                      initialValues={{ scopes: [...allowed] }}
                      onFinish={(values) =>
                        createKey.mutate({ accountId: account.id, scopes: values.scopes })
                      }
                    >
                      <Form.Item name="scopes" label={t("keyScopes")}>
                        <Checkbox.Group
                          options={allowed.map((name) => ({ value: name, label: name }))}
                        />
                      </Form.Item>
                      <Form.Item>
                        <Button htmlType="submit" loading={createKey.isPending}>
                          {t("newApiKey")}
                        </Button>
                      </Form.Item>
                    </Form>
                  ) : null}
                </div>
              </article>
            );
          })
        )}
      </Card>
    </>
  );
}
