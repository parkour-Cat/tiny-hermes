import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Avatar,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Layout,
  Modal,
  Space,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { t } from "../i18n/zh-CN";

type Workspace = {
  id: string;
  name: string;
  status: string;
};

type WorkspaceValues = {
  name: string;
};

const WORKSPACES_QUERY = ["workspaces"] as const;

export function WorkspacesPage() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<WorkspaceValues>();
  const [open, setOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const workspaces = useQuery({
    queryKey: WORKSPACES_QUERY,
    queryFn: () => api<Workspace[]>("/api/v1/workspaces"),
  });
  const createWorkspace = useMutation({
    mutationFn: (values: WorkspaceValues) =>
      api<Workspace>("/api/v1/workspaces", {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (created) => {
      queryClient.setQueryData<Workspace[]>(WORKSPACES_QUERY, (current = []) => [
        ...current,
        created,
      ]);
      setActionError(null);
      setOpen(false);
      form.resetFields();
    },
    onError: (caught) => {
      setActionError(caught instanceof Error ? caught.message : t("requestFailed"));
    },
  });

  async function logout(): Promise<void> {
    setActionError(null);
    try {
      await auth.logout();
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : t("requestFailed"));
    }
  }

  return (
    <Layout className="app-layout">
      <Layout.Header className="app-header">
        <Typography.Text className="header-brand">{t("appName")}</Typography.Text>
        <Space>
          <Avatar>{auth.user?.display_name.slice(0, 1).toUpperCase()}</Avatar>
          <div className="user-summary">
            <Typography.Text>{auth.user?.display_name}</Typography.Text>
            <Typography.Text type="secondary">{auth.user?.subject}</Typography.Text>
          </div>
          <Button onClick={() => void logout()}>{t("logout")}</Button>
        </Space>
      </Layout.Header>
      <Layout.Content className="workspace-content">
        <div className="page-heading">
          <div>
            <Typography.Title level={2}>{t("workspaceTitle")}</Typography.Title>
            <Typography.Paragraph type="secondary">{t("workspaceIntro")}</Typography.Paragraph>
          </div>
          <Button type="primary" onClick={() => setOpen(true)}>
            {t("newWorkspace")}
          </Button>
        </div>
        {actionError === null ? null : (
          <Alert className="page-alert" type="error" title={actionError} showIcon />
        )}
        {workspaces.isError ? (
          <Alert
            type="error"
            title={workspaces.error.message}
            action={<Button onClick={() => void workspaces.refetch()}>{t("retry")}</Button>}
            showIcon
          />
        ) : (
          <Card loading={workspaces.isPending} variant="borderless">
            {(workspaces.data ?? []).length === 0 ? (
              <Empty description={t("emptyWorkspaces")} />
            ) : (
              <div className="workspace-list" role="list">
                {(workspaces.data ?? []).map((workspace) => (
                  <article className="workspace-row" role="listitem" key={workspace.id}>
                    <Avatar shape="square">{workspace.name.slice(0, 1)}</Avatar>
                    <div className="workspace-summary">
                      <Typography.Title level={4}>{workspace.name}</Typography.Title>
                      <Typography.Text type="secondary">{workspace.id}</Typography.Text>
                    </div>
                  <Tag color="green">{t("workspaceActive")}</Tag>
                  </article>
                ))}
              </div>
            )}
          </Card>
        )}
      </Layout.Content>
      <Modal
        open={open}
        title={t("newWorkspace")}
        okText={t("create")}
        cancelText={t("cancel")}
        confirmLoading={createWorkspace.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => void form.submit()}
        destroyOnHidden
      >
        <Form<WorkspaceValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => createWorkspace.mutate(values)}
        >
          <Form.Item
            name="name"
            label={t("workspaceName")}
            rules={[
              { required: true, whitespace: true, message: t("required") },
              { max: 120, message: t("workspaceNameMaximum") },
            ]}
          >
            <Input autoFocus />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  );
}
