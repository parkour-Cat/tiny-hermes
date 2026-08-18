import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { ApiError, api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { HttpToolResponse, HttpToolVersionResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type ToolValues = {
  name: string;
  base_url: string;
  document: string;
  credential_ref?: string;
};

/**
 * Somebody else's API, registered so an Agent may be bound to part of it.
 *
 * The page shows every operation with `read_only` beside it, because that is
 * what decides whether calling it will stop for a person — and knowing that
 * before you bind is worth more than discovering it in a paused Run.
 *
 * A registration whose host the workspace has not approved is refused, and the
 * refusal names the host. The shape follows `outbound_entry_outside_platform`:
 * a message that only said "not allowed" would send the reader to the code,
 * while naming the host and the page that grants it sends them to the person
 * who can approve it.
 */
export function HttpToolsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [modal, contextHolder] = Modal.useModal();
  const [form] = Form.useForm<ToolValues>();
  const [error, setError] = useState<string | null>(null);
  const [missingHost, setMissingHost] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };

  const tools = useQuery({
    queryKey: ["http-tools", workspaceId] as const,
    queryFn: () => api<HttpToolResponse[]>("/api/v1/http-tools", scope),
    enabled: workspaceId !== null,
  });

  const versions = useQuery({
    queryKey: ["http-tool-versions", workspaceId, (tools.data ?? []).length] as const,
    enabled: (tools.data ?? []).length > 0,
    queryFn: async () =>
      Object.fromEntries(
        await Promise.all(
          (tools.data ?? []).map(async (tool) => [
            tool.id,
            await api<HttpToolVersionResponse[]>(
              `/api/v1/http-tools/${tool.id}/versions`,
              scope,
            ),
          ]),
        ),
      ) as Record<string, HttpToolVersionResponse[]>,
  });

  function refresh(): void {
    setError(null);
    setMissingHost(null);
    void queryClient.invalidateQueries({ queryKey: ["http-tools"] });
    void queryClient.invalidateQueries({ queryKey: ["http-tool-versions"] });
  }

  const register = useMutation({
    mutationFn: (values: ToolValues) =>
      api<HttpToolResponse>("/api/v1/http-tools", {
        ...scope,
        method: "POST",
        body: JSON.stringify({
          name: values.name,
          base_url: values.base_url,
          document: values.document,
          credential_ref: values.credential_ref?.trim() || null,
        }),
      }),
    onSuccess: () => {
      form.resetFields();
      refresh();
    },
    onError: (caught) => {
      setMissingHost(
        caught instanceof ApiError && caught.code === "host_outside_workspace_scope"
          ? problemMessage(caught)
          : null,
      );
      setError(problemMessage(caught));
    },
  });

  const withdraw = useMutation({
    mutationFn: (input: { toolId: string; versionId: string }) =>
      api<HttpToolVersionResponse>(
        `/api/v1/http-tools/${input.toolId}/versions/${input.versionId}/withdraw`,
        { ...scope, method: "POST" },
      ),
    onSuccess: refresh,
    onError: (caught) => setError(problemMessage(caught)),
  });

  if (tools.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(tools.error)}
        action={<Button onClick={() => void tools.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("httpToolsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("httpToolsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {missingHost === null ? null : (
        <Alert
          className="page-alert"
          type="info"
          title={missingHost}
          showIcon
        />
      )}

      <Card title={t("httpToolRegister")} variant="borderless" className="page-alert">
        <Form<ToolValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => register.mutate(values)}
        >
          <Form.Item
            name="name"
            label={t("httpToolName")}
            extra={t("httpToolNameHint")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="base_url"
            label={t("httpToolBaseUrl")}
            extra={t("httpToolBaseUrlHint")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="credential_ref"
            label={t("httpToolCredential")}
            extra={t("httpToolCredentialHint")}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="document"
            label={t("httpToolDocument")}
            extra={t("httpToolDocumentHint")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input.TextArea rows={8} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={register.isPending}>
              {t("httpToolRegister")}
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {(tools.data ?? []).length === 0 ? (
        <Empty description={t("emptyHttpTools")} />
      ) : (
        (tools.data ?? []).map((tool) => (
          <Card
            key={tool.id}
            title={
              <Space wrap>
                <Typography.Text strong>{tool.name}</Typography.Text>
                <Typography.Text type="secondary" code>
                  {tool.base_url}
                </Typography.Text>
              </Space>
            }
            variant="borderless"
            className="page-alert"
            loading={versions.isPending}
          >
            {(versions.data?.[tool.id] ?? []).map((version) => (
              <div key={version.id} className="skill-version-row">
                <Space wrap>
                  <Tag>v{version.version_number}</Tag>
                  <Typography.Text>{version.title}</Typography.Text>
                  {version.bindable ? null : <Tag color="default">{version.status}</Tag>}
                  {version.bindable ? (
                    <Button
                      size="small"
                      loading={withdraw.isPending}
                      onClick={() =>
                        void modal.confirm({
                          title: t("httpToolWithdraw"),
                          content: t("httpToolWithdrawWarning"),
                          okText: t("confirm"),
                          cancelText: t("cancel"),
                          onOk: () =>
                            withdraw
                              .mutateAsync({ toolId: tool.id, versionId: version.id })
                              .catch(() => undefined),
                        })
                      }
                    >
                      {t("httpToolWithdraw")}
                    </Button>
                  ) : null}
                </Space>
                <Space wrap size={[8, 4]}>
                  {version.operations.map((operation) =>
                    operation.read_only ? (
                      <Tag key={operation.operation_id}>
                        {operation.method} {operation.operation_id}
                      </Tag>
                    ) : (
                      /* Marked on the tag rather than explained in a legend:
                         whether a call will stop for a person is what you need
                         to know while choosing, not afterwards. */
                      <Tag key={operation.operation_id} color="orange">
                        {operation.method} {operation.operation_id} · {t("httpToolWrites")}
                      </Tag>
                    ),
                  )}
                </Space>
              </div>
            ))}
          </Card>
        ))
      )}
    </>
  );
}
