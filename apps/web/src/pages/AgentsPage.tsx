import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Avatar, Button, Card, Form, Input, Modal, Select, Space, Typography } from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { problemField, problemMessage } from "../api/messages";
import type {
  AgentExampleResponse,
  AgentResponse,
  CreatedExampleResponse,
  ModelEndpointSummary,
} from "../api/types";
import { useT } from "../i18n/locale";
import { StatusTag } from "../ui/StatusTag";
import { EmptyState } from "../ui/EmptyState";
import { PageHeading } from "../ui/PageHeading";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type AgentValues = {
  name: string;
  alias: string;
};

export function AgentsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [form] = Form.useForm<AgentValues>();
  const [open, setOpen] = useState(false);
  const agentsQuery = ["agents", workspaceId] as const;
  const agents = useQuery({
    queryKey: agentsQuery,
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null,
  });
  // Fetched only while the workspace is empty: §21 offers the example at the
  // end of setup, and a workspace that already has Agents has passed that
  // point. Two requests on every visit to a busy list would be paid forever
  // for a card nobody sees.
  const nothingYet = (agents.data ?? []).length === 0 && !agents.isPending;
  const examples = useQuery({
    queryKey: ["agent-examples"] as const,
    queryFn: () => api<AgentExampleResponse[]>("/api/v1/agents/examples"),
    enabled: nothingYet,
  });
  const endpoints = useQuery({
    queryKey: ["model-endpoints"] as const,
    queryFn: () => api<ModelEndpointSummary[]>("/api/v1/model-endpoints"),
    enabled: nothingYet,
  });
  const [endpointId, setEndpointId] = useState<string | null>(null);
  const available = endpoints.data ?? [];
  // The chosen one, or the only one. Never a hardcoded id: publishing would
  // fail on every deployment but the one it was written against.
  const chosen = endpointId ?? available[0]?.id ?? null;
  const [exampleError, setExampleError] = useState<string | null>(null);

  const createExample = useMutation({
    mutationFn: (slug: string) =>
      api<CreatedExampleResponse>(`/api/v1/agents/examples/${slug}`, {
        method: "POST",
        workspace: workspaceId ?? "",
        body: JSON.stringify({ endpoint_id: chosen }),
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: agentsQuery });
      // Into the builder, like `createAgent` — the example is a thing to read
      // and adapt, and a list row in small type does not invite that.
      navigate(`/workspaces/${workspaceId ?? ""}/agents/${created.agent.id}`);
    },
    onError: (caught) => setExampleError(problemMessage(caught, t)),
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
      // Straight into the builder. A new Agent has no persona, no model and no
      // tools yet, so dropping the person back on a list — where the only way
      // in is the name, in small type — leaves the next step to be guessed.
      navigate(`/workspaces/${workspaceId ?? ""}/agents/${created.id}`);
    },
    onError: (caught) => {
      // The dialog stays open with the typed values in it. A refused name or
      // alias is something the user can edit; throwing the input away would
      // make a recoverable refusal cost a retype.
      const field: keyof AgentValues = problemField(caught) === "name" ? "name" : "alias";
      form.setFields([{ name: field, errors: [problemMessage(caught, t)] }]);
    },
  });

  if (agents.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(agents.error, t)}
        action={<Button onClick={() => void agents.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      <PageHeading
        kicker={t("workspaceTitle")}
        title={t("agentsTitle")}
        intro={t("agentsIntro")}
        extra={
          <Button type="primary" onClick={() => setOpen(true)}>
            {t("newAgent")}
          </Button>
        }
      />
      <Card loading={agents.isPending} variant="borderless">
        {(agents.data ?? []).length === 0 ? (
          <Space direction="vertical" size="large" style={{ width: "100%" }}>
            <EmptyState title={t("emptyAgents")} />
            {/* The path from nothing, said where a person with nothing is
                standing. Step one links to where it happens and says whether
                it is already done, because the example below cannot be
                created without it. */}
            <Card variant="borderless" className="page-alert" title={t("onboardingTitle")}>
              <ol className="onboarding">
                <li>
                  <Link to={`/workspaces/${workspaceId}/settings#model-endpoints`}>
                    {t("onboardingStep1")}
                  </Link>{" "}
                  <Typography.Text type="secondary">
                    {available.length === 0 ? t("onboardingStep1Todo") : t("onboardingStep1Done")}
                  </Typography.Text>
                </li>
                <li>{t("onboardingStep2")}</li>
                <li>{t("onboardingStep3")}</li>
              </ol>
            </Card>
            {(examples.data ?? []).length === 0 ? null : (
              <Card variant="borderless" className="page-alert" title={t("exampleAgentTitle")}>
                <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                  <Typography.Paragraph type="secondary">
                    {t("exampleAgentIntro")}
                  </Typography.Paragraph>
                  {exampleError === null ? null : (
                    <Alert type="error" showIcon message={exampleError} />
                  )}
                  {available.length === 0 ? (
                    // Said before the button rather than after the failure:
                    // §21 configures a model alias before this step, and a
                    // button that can only fail teaches nothing.
                    <Alert type="info" showIcon message={t("exampleAgentNeedsEndpoint")} />
                  ) : (
                    <>
                      <Select
                        aria-label={t("exampleAgentEndpoint")}
                        style={{ minWidth: 260 }}
                        value={chosen}
                        onChange={(value: string) => setEndpointId(value)}
                        options={available.map((endpoint) => ({
                          value: endpoint.id,
                          label: `${endpoint.name} · ${endpoint.model}`,
                        }))}
                      />
                      {(examples.data ?? []).map((example) => (
                        <Space key={example.slug} direction="vertical" size={4}>
                          <Typography.Text strong>{example.name}</Typography.Text>
                          <Typography.Text type="secondary">{example.summary}</Typography.Text>
                          <Button
                            loading={createExample.isPending}
                            onClick={() => createExample.mutate(example.slug)}
                          >
                            {t("exampleAgentCreate")}
                          </Button>
                        </Space>
                      ))}
                    </>
                  )}
                </Space>
              </Card>
            )}
          </Space>
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
                  <div>
                    {/* An unpublished Agent cannot answer anything, so the
                        playground is a dead end until it is configured. */}
                    {entry.current_version_id === null ? (
                      <Link to={`/workspaces/${workspaceId}/agents/${entry.id}`}>
                        {t("configureAgent")}
                      </Link>
                    ) : (
                      <Link to={`/workspaces/${workspaceId}/agents/${entry.id}/playground`}>
                        {t("playground")}
                      </Link>
                    )}
                  </div>
                </div>
                <StatusTag code={entry.current_version_id === null ? "unpublished" : "published"} />
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
