import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Avatar, Button, Card, Form, Input, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { Link, NavLink } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";
import { BrandMark, ConsoleChrome, PageHeading } from "../layout/ConsoleChrome";
import { EmptyState } from "../ui/EmptyState";

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
  const t = useT();
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

  return (
    <ConsoleChrome
      sidebar={
        <>
          <BrandMark />
          <div className="th-workspace-chip">{auth.user?.display_name}</div>
          <nav className="th-nav">
            <NavLink to="/workspaces" className="th-nav-link active">
              {t("workspaceTitle")}
            </NavLink>
          </nav>
        </>
      }
    >
      <PageHeading
        kicker={t("appKicker")}
        title={t("workspaceTitle")}
        intro={t("workspaceIntro")}
        extra={
          <Button type="primary" onClick={() => setOpen(true)}>
            {t("newWorkspace")}
          </Button>
        }
      />
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
      ) : workspaces.isPending ? (
        <Card loading variant="borderless" />
      ) : (workspaces.data ?? []).length === 0 ? (
        <Card variant="borderless">
          <EmptyState title={t("emptyWorkspaces")} />
        </Card>
      ) : (
        <Card variant="borderless">
          <div className="workspace-list" role="list">
            {(workspaces.data ?? []).map((workspace) => (
              <article className="workspace-row" role="listitem" key={workspace.id}>
                <Avatar shape="square">{workspace.name.slice(0, 1)}</Avatar>
                <div className="workspace-summary">
                  <Typography.Title level={4}>
                    <Link to={`/workspaces/${workspace.id}/agents`}>{workspace.name}</Link>
                  </Typography.Title>
                </div>
                <Space>
                  <Tag className="th-tag th-tag-active">{t("workspaceActive")}</Tag>
                  <Link to={`/workspaces/${workspace.id}/agents`}>
                    <Button type="link">{t("openWorkspace")}</Button>
                  </Link>
                </Space>
              </article>
            ))}
          </div>
        </Card>
      )}
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
    </ConsoleChrome>
  );
}
