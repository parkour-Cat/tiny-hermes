import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { api, apiWithStatus } from "../api/client";
import { problemMessage } from "../api/messages";
import type { McpServerResponse, McpServerVersionResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type ServerValues = { name: string; url: string; credential_ref?: string };

/**
 * MCP servers, and what each of them said it could do.
 *
 * Registering reads the server rather than accepting a document, so this page
 * has a "read again" button and no upload field. Two facts are therefore shown
 * per server: when it was last successfully read, and what it advertised then.
 * "Registered" and "reachable" are different things, and only one of them is a
 * promise.
 *
 * Reading again adds a version only when something changed. The point of a
 * version is that somebody reviewed it, and a snapshot identical to the last
 * one has nothing new to review — so an unchanged read says so instead of
 * growing the list.
 */
export function McpServersPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<ServerValues>();
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };

  const servers = useQuery({
    queryKey: ["mcp-servers", workspaceId] as const,
    queryFn: () => api<McpServerResponse[]>("/api/v1/mcp-servers", scope),
    enabled: workspaceId !== null,
  });

  const versions = useQuery({
    queryKey: ["mcp-server-versions", workspaceId, (servers.data ?? []).length] as const,
    enabled: (servers.data ?? []).length > 0,
    queryFn: async () =>
      Object.fromEntries(
        await Promise.all(
          (servers.data ?? []).map(async (server) => [
            server.id,
            await api<McpServerVersionResponse[]>(
              `/api/v1/mcp-servers/${server.id}/versions`,
              scope,
            ),
          ]),
        ),
      ) as Record<string, McpServerVersionResponse[]>,
  });

  function refresh(): void {
    setError(null);
    void queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
    void queryClient.invalidateQueries({ queryKey: ["mcp-server-versions"] });
  }

  const register = useMutation({
    mutationFn: (values: ServerValues) =>
      api<McpServerResponse>("/api/v1/mcp-servers", {
        ...scope,
        method: "POST",
        body: JSON.stringify({
          name: values.name,
          url: values.url,
          credential_ref: values.credential_ref?.trim() || null,
        }),
      }),
    onSuccess: () => {
      setNote(null);
      form.resetFields();
      refresh();
    },
    onError: (caught) => {
      setNote(null);
      setError(problemMessage(caught));
    },
  });

  const reread = useMutation({
    mutationFn: (serverId: string) =>
      apiWithStatus<McpServerVersionResponse>(
        `/api/v1/mcp-servers/${serverId}/refresh`,
        { ...scope, method: "POST" },
      ),
    // 200 means the snapshot was identical and no version was added. Said in
    // words, because otherwise a person who clicked and saw nothing change
    // cannot tell that from a button that did nothing.
    onSuccess: (result) => {
      setNote(result.status === 200 ? t("mcpServerUnchanged") : null);
      refresh();
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  if (servers.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(servers.error)}
        action={<Button onClick={() => void servers.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("mcpServersTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("mcpServersIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {note === null ? null : (
        <Alert className="page-alert" type="info" title={note} showIcon />
      )}

      <Card title={t("httpToolRegister")} variant="borderless" className="page-alert">
        <Form<ServerValues>
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
            name="url"
            label={t("mcpServerUrl")}
            extra={t("mcpServerUrlHint")}
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
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={register.isPending}>
              {t("httpToolRegister")}
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {(servers.data ?? []).length === 0 ? (
        <Empty description={t("emptyMcpServers")} />
      ) : (
        (servers.data ?? []).map((server) => (
          <Card
            key={server.id}
            title={
              <Space wrap>
                <Typography.Text strong>{server.name}</Typography.Text>
                <Typography.Text type="secondary" code>
                  {server.url}
                </Typography.Text>
              </Space>
            }
            extra={
              <Button
                size="small"
                loading={reread.isPending}
                onClick={() => reread.mutate(server.id)}
              >
                {t("mcpServerRefresh")}
              </Button>
            }
            variant="borderless"
            className="page-alert"
            loading={versions.isPending}
          >
            <Typography.Paragraph type="secondary">
              {t("mcpServerValidated")}:{" "}
              {server.last_validated_at === null
                ? t("mcpServerNever")
                : moment(server.last_validated_at)}
            </Typography.Paragraph>
            {(versions.data?.[server.id] ?? []).map((version) => (
              <div key={version.id} className="skill-version-row">
                <Space wrap>
                  <Tag>v{version.version_number}</Tag>
                  <Typography.Text type="secondary">{t("mcpServerTools")}</Typography.Text>
                  {version.bindable ? null : <Tag color="default">{version.status}</Tag>}
                </Space>
                <Space wrap size={[8, 4]}>
                  {version.tools.map((tool) => (
                    <Tag key={tool.name}>{tool.name}</Tag>
                  ))}
                </Space>
              </div>
            ))}
          </Card>
        ))
      )}
    </>
  );
}
