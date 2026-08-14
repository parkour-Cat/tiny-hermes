import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Typography } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { WorkspaceMemberResponse } from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { PageHeading } from "../layout/ConsoleChrome";
import { EmptyState } from "../ui/EmptyState";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type InviteValues = {
  email: string;
  role: "workspace_admin" | "developer" | "viewer";
};

const ROLE_KEYS: Record<InviteValues["role"], MessageKey> = {
  workspace_admin: "memberRoleAdmin",
  developer: "memberRoleDeveloper",
  viewer: "memberRoleViewer",
};

export function MembersPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<InviteValues>();
  const [modal, contextHolder] = Modal.useModal();
  const [error, setError] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const membersQuery = ["workspace-members", workspaceId] as const;

  const members = useQuery({
    queryKey: membersQuery,
    queryFn: () =>
      api<WorkspaceMemberResponse[]>(`/api/v1/workspaces/${workspaceId}/members`, scope),
    enabled: workspaceId !== null,
  });

  const invite = useMutation({
    mutationFn: (values: InviteValues) =>
      api<WorkspaceMemberResponse>(`/api/v1/workspaces/${workspaceId}/members`, {
        ...scope,
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (created) => {
      queryClient.setQueryData<WorkspaceMemberResponse[]>(membersQuery, (current = []) => [
        ...current.filter((member) => member.user_id !== created.user_id),
        created,
      ]);
      form.resetFields();
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  const changeRole = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: InviteValues["role"] }) =>
      api<WorkspaceMemberResponse>(`/api/v1/workspaces/${workspaceId}/members/${userId}`, {
        ...scope,
        method: "PATCH",
        body: JSON.stringify({ role }),
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData<WorkspaceMemberResponse[]>(membersQuery, (current = []) =>
        current.map((member) => (member.user_id === updated.user_id ? updated : member)),
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  const remove = useMutation({
    mutationFn: (userId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/members/${userId}`, {
        ...scope,
        method: "DELETE",
      }),
    onSuccess: (_void, userId) => {
      queryClient.setQueryData<WorkspaceMemberResponse[]>(membersQuery, (current = []) =>
        current.filter((member) => member.user_id !== userId),
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  if (members.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(members.error)}
        action={<Button onClick={() => void members.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  const roles = (Object.keys(ROLE_KEYS) as InviteValues["role"][]).map((role) => ({
    value: role,
    label: t(ROLE_KEYS[role]),
  }));

  return (
    <>
      {contextHolder}
      <PageHeading kicker={t("workspaceTitle")} title={t("members")} />
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      <Card title={t("inviteMember")} variant="borderless" className="page-alert">
        <Form<InviteValues>
          form={form}
          layout="inline"
          requiredMark={false}
          initialValues={{ role: "developer" }}
          onFinish={(values) => invite.mutate(values)}
        >
          <Form.Item
            name="email"
            label={t("memberEmail")}
            rules={[
              { required: true, message: t("required") },
              { type: "email", message: t("invalidEmail") },
            ]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="role" label={t("memberRole")} rules={[{ required: true }]}>
            <Select options={roles} popupMatchSelectWidth={false} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={invite.isPending}>
              {t("inviteMember")}
            </Button>
          </Form.Item>
        </Form>
      </Card>
      {members.isPending ? (
        <Card loading variant="borderless" />
      ) : (members.data ?? []).length === 0 ? (
        <Card variant="borderless">
          <EmptyState title={t("emptyMembers")} />
        </Card>
      ) : (
        <Card variant="borderless">
          {(members.data ?? []).map((member) => (
            <Space key={member.user_id} className="workspace-row" wrap>
              <div className="workspace-summary">
                <Typography.Text strong>{member.display_name}</Typography.Text>
                <Typography.Paragraph type="secondary">{member.subject}</Typography.Paragraph>
              </div>
              <Select
                aria-label={`${member.subject} ${t("memberRole")}`}
                value={member.role as InviteValues["role"]}
                options={roles}
                onChange={(role: InviteValues["role"]) =>
                  changeRole.mutate({ userId: member.user_id, role })
                }
              />
              <Button
                onClick={() =>
                  void modal.confirm({
                    title: t("removeMember"),
                    content: t("removeMemberWarning"),
                    okText: t("confirm"),
                    cancelText: t("cancel"),
                    onOk: () => remove.mutateAsync(member.user_id).catch(() => undefined),
                  })
                }
              >
                {t("removeMember")}
              </Button>
            </Space>
          ))}
        </Card>
      )}
    </>
  );
}
