import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Avatar, Button, Card, Form, Input, Modal, Space, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useT } from "../i18n/locale";
import { EmptyState } from "../ui/EmptyState";
import { PageHeading } from "../ui/PageHeading";
import { StatusTag } from "../ui/StatusTag";
import { BrandMark, ConsoleChrome } from "../layout/ConsoleChrome";
import { useAuth } from "../auth/AuthProvider";

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
          {/* No workspace yet, so the chip names the person: the same slot,
              the one thing that is certain before a workspace is chosen. */}
          <div className="th-workspace-chip">{auth.user?.display_name}</div>
          <nav className="th-nav" aria-label={t("workspaceTitle")}>
            <span className="th-nav-link active">{t("workspaceTitle")}</span>
          </nav>
        </>
      }
    >
      <>
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
        ) : (
          <Card loading={workspaces.isPending} variant="borderless">
            {(workspaces.data ?? []).length === 0 ? (
              // Two different situations that look identical from here. A
              // platform administrator with no workspaces has not made one
              // yet and can; anybody else — including a user just created
              // by an OIDC login, who has no Membership at all — is waiting
              // on somebody and can do nothing about it. "还没有工作空间"
              // reads to the second as "this system is empty", which sends
              // them looking for a bug that is not there.
              auth.user?.is_platform_admin === false ? (
                <EmptyState
                  title={
                    <Space orientation="vertical" size={4}>
                      <Typography.Text>{t("noWorkspacesTitle")}</Typography.Text>
                      <Typography.Text type="secondary">{t("noWorkspacesBody")}</Typography.Text>
                    </Space>
                  }
                />
              ) : (
                <EmptyState title={t("emptyWorkspaces")} />
              )
            ) : (
              <div className="workspace-list" role="list">
                {(workspaces.data ?? []).map((workspace) => (
                  <article className="workspace-row" role="listitem" key={workspace.id}>
                    <Avatar shape="square">{workspace.name.slice(0, 1)}</Avatar>
                    {/* The name, and only the name. The id is what a URL carries;
                        printed here it took the widest line in the row and nobody
                        read it (§4.1). */}
                    <div className="workspace-summary">
                      <Typography.Title level={4}>
                        <Link to={`/workspaces/${workspace.id}/agents`}>{workspace.name}</Link>
                      </Typography.Title>
                    </div>
                    <Space>
                      <StatusTag code={workspace.status} />
                      <Link to={`/workspaces/${workspace.id}/agents`}>
                        <Button type="link">{t("openWorkspace")}</Button>
                      </Link>
                    </Space>
                  </article>
                ))}
              </div>
            )}
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
      </>
    </ConsoleChrome>
  );
}
