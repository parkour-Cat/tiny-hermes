import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Avatar, Button, Card, Empty, Form, Input, Modal, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { problemField, problemMessage } from "../api/messages";
import type { AgentResponse } from "../api/types";
import { t } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type AgentValues = {
  name: string;
  alias: string;
};

export function AgentsPage() {
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<AgentValues>();
  const [open, setOpen] = useState(false);
  const agentsQuery = ["agents", workspaceId] as const;
  const agents = useQuery({
    queryKey: agentsQuery,
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null,
  });
  const createAgent = useMutation({
    mutationFn: (values: AgentValues) =>
      api<AgentResponse>("/api/v1/agents", {
        method: "POST",
        workspace: workspaceId ?? "",
        body: JSON.stringify(values),
      }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentResponse[]>(agentsQuery, (current = []) => [
        ...current,
        created,
      ]);
      setOpen(false);
      form.resetFields();
    },
    onError: (caught) => {
      // The dialog stays open with the typed values in it. A refused name or
      // alias is something the user can edit; throwing the input away would
      // make a recoverable refusal cost a retype.
      const field: keyof AgentValues = problemField(caught) === "name" ? "name" : "alias";
      form.setFields([{ name: field, errors: [problemMessage(caught)] }]);
    },
  });

  if (agents.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(agents.error)}
        action={<Button onClick={() => void agents.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("agentsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("agentsIntro")}</Typography.Paragraph>
        </div>
        <Button type="primary" onClick={() => setOpen(true)}>
          {t("newAgent")}
        </Button>
      </div>
      <Card loading={agents.isPending} variant="borderless">
        {(agents.data ?? []).length === 0 ? (
          <Empty description={t("emptyAgents")} />
        ) : (
          <div className="workspace-list" role="list">
            {(agents.data ?? []).map((entry) => (
              <article className="workspace-row" role="listitem" aria-label={entry.name} key={entry.id}>
                <Avatar shape="square">{entry.name.slice(0, 1)}</Avatar>
                <div className="workspace-summary">
                  <Typography.Title level={4}>
                    <Link to={`/workspaces/${workspaceId}/agents/${entry.id}`}>{entry.name}</Link>
                  </Typography.Title>
                  <Typography.Text type="secondary">{entry.alias}</Typography.Text>
                </div>
                <Tag color={entry.current_version_id === null ? "default" : "green"}>
                  {entry.current_version_id === null ? t("agentUnpublished") : t("agentPublished")}
                </Tag>
              </article>
            ))}
          </div>
        )}
      </Card>
      <Modal
        open={open}
        title={t("newAgent")}
        okText={t("create")}
        cancelText={t("cancel")}
        confirmLoading={createAgent.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => void form.submit()}
        destroyOnHidden
      >
        <Form<AgentValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => createAgent.mutate(values)}
        >
          <Form.Item
            name="name"
            label={t("agentName")}
            rules={[
              { required: true, whitespace: true, message: t("required") },
              { max: 120, message: t("agentNameMaximum") },
            ]}
          >
            <Input autoFocus />
          </Form.Item>
          <Form.Item
            name="alias"
            label={t("agentAlias")}
            extra={t("agentAliasHint")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
