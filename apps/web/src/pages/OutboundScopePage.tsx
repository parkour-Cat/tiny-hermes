import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { OutboundScopeEntry } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type EntryValues = { entry: string; note?: string };

/**
 * The two levels, in the order they constrain each other.
 *
 * The platform's list is shown to every workspace administrator even though
 * only a platform administrator may change it: they are choosing inside it, and
 * a range you cannot see is a range you guess at. The workspace's own list
 * comes second because that is the direction the rule runs — each layer narrows
 * the one above and none may widen it.
 */
export function OutboundScopePage() {
  const t = useT();
  const auth = useAuth();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [modal, contextHolder] = Modal.useModal();
  const [platformForm] = Form.useForm<EntryValues>();
  const [workspaceForm] = Form.useForm<EntryValues>();
  const [error, setError] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const admin = auth.user?.is_platform_admin === true;
  const platformQuery = ["outbound-scopes", "platform"] as const;
  const workspaceQuery = ["outbound-scopes", "workspace", workspaceId] as const;

  const platform = useQuery({
    queryKey: platformQuery,
    queryFn: () => api<OutboundScopeEntry[]>("/api/v1/outbound-scopes/platform", scope),
    enabled: workspaceId !== null,
  });
  const workspace = useQuery({
    queryKey: workspaceQuery,
    queryFn: () => api<OutboundScopeEntry[]>("/api/v1/outbound-scopes/workspace", scope),
    enabled: workspaceId !== null,
  });

  function refresh(): void {
    setError(null);
    void queryClient.invalidateQueries({ queryKey: ["outbound-scopes"] });
  }

  const approvePlatform = useMutation({
    mutationFn: (values: EntryValues) =>
      api<OutboundScopeEntry>("/api/v1/outbound-scopes/platform", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ entry: values.entry, note: values.note ?? null }),
      }),
    onSuccess: () => {
      platformForm.resetFields();
      refresh();
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const approveWorkspace = useMutation({
    mutationFn: (values: EntryValues) =>
      api<OutboundScopeEntry>("/api/v1/outbound-scopes/workspace", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ entry: values.entry, note: values.note ?? null }),
      }),
    onSuccess: () => {
      workspaceForm.resetFields();
      refresh();
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const revoke = useMutation({
    mutationFn: (entryId: string) =>
      api<void>(`/api/v1/outbound-scopes/${entryId}`, { ...scope, method: "DELETE" }),
    onSuccess: refresh,
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  if (platform.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(platform.error, t)}
        action={<Button onClick={() => void platform.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  function rows(entries: OutboundScopeEntry[], removable: boolean) {
    return entries.map((item) => (
      <Space key={item.id} wrap className="skill-version-row">
        <Typography.Text code>{item.entry}</Typography.Text>
        {item.note === null ? null : (
          <Typography.Text type="secondary">{item.note}</Typography.Text>
        )}
        {item.managed ? <Tag>{t("outboundManaged")}</Tag> : null}
        {/* Absent rather than disabled for an entry a model endpoint owns: the
            endpoint would put it straight back, so a control here could only
            ever lose. */}
        {removable && !item.managed ? (
          <Button
            size="small"
            loading={revoke.isPending}
            onClick={() =>
              void modal.confirm({
                title: t("outboundRevoke"),
                content: t("outboundRevokeWarning"),
                okText: t("confirm"),
                cancelText: t("cancel"),
                onOk: () => revoke.mutateAsync(item.id).catch(() => undefined),
              })
            }
          >
            {t("outboundRevoke")}
          </Button>
        ) : null}
      </Space>
    ));
  }

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Paragraph type="secondary">{t("outboundIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}

      <Card
        title={t("outboundPlatform")}
        variant="borderless"
        className="page-alert"
        loading={platform.isPending}
      >
        <Typography.Paragraph type="secondary">
          {t("outboundPlatformIntro")}
        </Typography.Paragraph>
        {admin ? (
          <Form<EntryValues>
            form={platformForm}
            // Named, and so is the workspace form below. Without distinct
            // names Ant Design generates the same control id for both `entry`
            // fields, and the second form's label then points at the first
            // form's input — wrong for a screen reader, and the reason an
            // acceptance walk could fill one of the two and not the other.
            name="platform-scope"
            layout="inline"
            requiredMark={false}
            onFinish={(values) => approvePlatform.mutate(values)}
          >
            <Form.Item
              name="entry"
              label={t("outboundEntry")}
              extra={t("outboundEntryHint")}
              rules={[{ required: true, whitespace: true, message: t("required") }]}
            >
              <Input style={{ minWidth: 260 }} />
            </Form.Item>
            <Form.Item name="note" label={t("outboundNote")}>
              <Input />
            </Form.Item>
            <Form.Item>
              <Button type="primary" htmlType="submit" loading={approvePlatform.isPending}>
                {t("outboundApprove")}
              </Button>
            </Form.Item>
          </Form>
        ) : null}
        {(platform.data ?? []).length === 0 ? (
          <Empty description={t("emptyOutboundPlatform")} />
        ) : (
          rows(platform.data ?? [], admin)
        )}
      </Card>

      <Card
        title={t("outboundWorkspace")}
        variant="borderless"
        loading={workspace.isPending}
      >
        <Typography.Paragraph type="secondary">
          {t("outboundWorkspaceIntro")}
        </Typography.Paragraph>
        <Form<EntryValues>
          form={workspaceForm}
          name="workspace-scope"
          layout="inline"
          requiredMark={false}
          onFinish={(values) => approveWorkspace.mutate(values)}
        >
          <Form.Item
            name="entry"
            label={t("outboundEntry")}
            extra={t("outboundEntryHint")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input style={{ minWidth: 260 }} />
          </Form.Item>
          <Form.Item name="note" label={t("outboundNote")}>
            <Input />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={approveWorkspace.isPending}>
              {t("outboundApprove")}
            </Button>
          </Form.Item>
        </Form>
        {(workspace.data ?? []).length === 0 ? (
          <Empty description={t("emptyOutboundWorkspace")} />
        ) : (
          rows(workspace.data ?? [], true)
        )}
      </Card>
    </>
  );
}
