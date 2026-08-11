import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Typography,
} from "antd";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError, api, apiWithStatus } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  AgentDraftResponse,
  AgentResponse,
  AgentSpecDocument,
  AgentVersionResponse,
  ModelEndpointSummary,
} from "../api/types";
import { MODEL_SCENARIOS } from "../api/types";
import { t } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type DraftValues = {
  personality: string;
  provider: "deterministic" | "openai_compatible";
  scenario: string;
  endpoint_id: string | undefined;
  max_execution_seconds: number;
  max_elapsed_seconds: number;
  max_model_calls: number;
  max_tool_calls: number;
  max_derived_retries: number;
};

function valuesOf(draft: AgentDraftResponse): DraftValues {
  const policy = draft.spec.model_policy;
  return {
    personality: draft.spec.personality,
    provider: policy.provider,
    // Both fields are kept in the form whichever provider is selected, so
    // switching back and forth does not lose what was already chosen.
    scenario: policy.provider === "deterministic" ? policy.scenario : "complete",
    endpoint_id: policy.provider === "openai_compatible" ? policy.endpoint_id : undefined,
    ...draft.spec.limits,
  };
}

function specOf(values: DraftValues): AgentSpecDocument {
  return {
    schema_version: 1,
    personality: values.personality,
    // Built from the selection rather than echoed from the loaded spec, so the
    // console never carries forward a shape it could not have produced.
    model_policy:
      values.provider === "openai_compatible"
        ? { provider: "openai_compatible", endpoint_id: values.endpoint_id ?? "" }
        : { provider: "deterministic", scenario: values.scenario },
    tools: [],
    limits: {
      max_execution_seconds: values.max_execution_seconds,
      max_elapsed_seconds: values.max_elapsed_seconds,
      max_model_calls: values.max_model_calls,
      max_tool_calls: values.max_tool_calls,
      max_derived_retries: values.max_derived_retries,
    },
  };
}

/** The server's own bounds, so a refusal is rare rather than routine. */
const LIMITS: { name: keyof DraftValues; label: string; min: number; max: number }[] = [
  { name: "max_execution_seconds", label: t("maxExecutionSeconds"), min: 1, max: 900 },
  { name: "max_elapsed_seconds", label: t("maxElapsedSeconds"), min: 60, max: 86_400 },
  { name: "max_model_calls", label: t("maxModelCalls"), min: 1, max: 20 },
  { name: "max_tool_calls", label: t("maxToolCalls"), min: 0, max: 50 },
  { name: "max_derived_retries", label: t("maxDerivedRetries"), min: 0, max: 3 },
];

export function AgentDetailPage() {
  const workspaceId = useWorkspaceId();
  const { agentId = "" } = useParams();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<DraftValues>();
  const [modal, contextHolder] = Modal.useModal();
  const [saveError, setSaveError] = useState<string | null>(null);
  const [publishNote, setPublishNote] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const enabled = workspaceId !== null && agentId !== "";

  const agent = useQuery({
    queryKey: ["agent", workspaceId, agentId] as const,
    queryFn: () => api<AgentResponse>(`/api/v1/agents/${agentId}`, scope),
    enabled,
  });
  const draftQuery = ["agent-draft", workspaceId, agentId] as const;
  const draft = useQuery({
    queryKey: draftQuery,
    queryFn: () => api<AgentDraftResponse>(`/api/v1/agents/${agentId}/draft`, scope),
    enabled,
  });
  // Platform-wide rather than workspace-scoped: an endpoint is approved by a
  // platform administrator and every workspace chooses from the same list.
  const endpoints = useQuery({
    queryKey: ["model-endpoints"] as const,
    queryFn: () => api<ModelEndpointSummary[]>("/api/v1/model-endpoints", scope),
  });
  const provider = Form.useWatch("provider", form);
  const agentQuery = ["agent", workspaceId, agentId] as const;
  const versionsQuery = ["agent-versions", workspaceId, agentId] as const;
  const versions = useQuery({
    queryKey: versionsQuery,
    queryFn: () => api<AgentVersionResponse[]>(`/api/v1/agents/${agentId}/versions`, scope),
    enabled,
  });

  const saveDraft = useMutation({
    mutationFn: (values: DraftValues) =>
      api<AgentDraftResponse>(`/api/v1/agents/${agentId}/draft`, {
        ...scope,
        method: "PUT",
        body: JSON.stringify({
          expected_revision: draft.data?.revision ?? 0,
          spec: specOf(values),
        }),
      }),
    onSuccess: (saved) => {
      queryClient.setQueryData(draftQuery, saved);
      setSaveError(null);
    },
    // No refetch-and-retry. Saving again with the revision the server just
    // reported would overwrite whatever the other writer stored and report it
    // as a success — a lost update wearing a success message. The conflict is
    // the user's to resolve, and their text stays in the form while they do.
    onError: (caught) => setSaveError(problemMessage(caught)),
  });

  const publish = useMutation({
    mutationFn: (expectedRevision: number) =>
      apiWithStatus<AgentVersionResponse>(`/api/v1/agents/${agentId}/publish`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      }),
    onSuccess: ({ data: version, status }) => {
      queryClient.setQueryData<AgentResponse>(agentQuery, (current) =>
        current === undefined
          ? current
          : { ...current, status: "published", current_version_id: version.id },
      );
      queryClient.setQueryData<AgentVersionResponse[]>(versionsQuery, (current = []) =>
        current.some((entry) => entry.id === version.id) ? current : [...current, version],
      );
      // 200 means this content was already published. The server calls that
      // success, so the console reports what happened rather than dressing it
      // up as a new version or as a failure.
      setPublishNote(status === 200 ? t("publishUnchanged") : null);
      setSaveError(null);
    },
    onError: (caught) => setSaveError(problemMessage(caught)),
  });

  const failed = [agent, draft, versions].find((query) => query.isError);
  if (failed !== undefined) {
    return (
      <Alert
        type="error"
        title={problemMessage(failed.error)}
        action={<Button onClick={() => void failed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }
  if (agent.data === undefined || draft.data === undefined) {
    return <Card loading variant="borderless" />;
  }

  function reload(): void {
    // Asked before it happens, because the draft in the form may be the only
    // copy of what the user wrote.
    void modal.confirm({
      title: t("reloadDraft"),
      content: t("reloadDraftWarning"),
      okText: t("confirm"),
      cancelText: t("cancel"),
      onOk: async () => {
        const fresh = await draft.refetch();
        if (fresh.data !== undefined) {
          form.setFieldsValue(valuesOf(fresh.data));
        }
        setSaveError(null);
      },
    });
  }

  function askToPublish(revision: number): void {
    void modal.confirm({
      title: t("publish"),
      content: `${t("publishWarningPrefix")}${revision}${t("publishWarningSuffix")}`,
      okText: t("confirm"),
      cancelText: t("cancel"),
      // The question closes whether or not the publish succeeded: a refusal is
      // reported on the page, and leaving the dialog open would cover the very
      // message the user has to read.
      onOk: () => publish.mutateAsync(revision).catch(() => undefined),
    });
  }

  const published = versions.data?.find((version) => version.id === agent.data?.current_version_id);
  const lastError = saveDraft.error ?? publish.error;
  const conflicted =
    lastError instanceof ApiError && lastError.code === "draft_revision_conflict";

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{agent.data.name}</Typography.Title>
          <Typography.Paragraph type="secondary">{agent.data.alias}</Typography.Paragraph>
        </div>
        <Button
          type="primary"
          loading={publish.isPending}
          onClick={() => askToPublish(draft.data?.revision ?? 0)}
        >
          {t("publish")}
        </Button>
      </div>
      <Card variant="borderless" className="page-alert">
        {/* Two facts, side by side and unrelated. Stated rather than compared:
            the draft endpoint returns no content hash, so "unchanged since
            publish" is not something this console can know. */}
        <Space size="large" wrap>
          <Typography.Text strong>
            {`${t("draftRevision")} ${draft.data.revision}`}
          </Typography.Text>
          {agent.data.current_version_id === null ? (
            <Typography.Text>{t("agentUnpublished")}</Typography.Text>
          ) : (
            <Typography.Text>
              {`${t("currentVersion")} v${published?.version_number ?? "?"}`}
            </Typography.Text>
          )}
        </Space>
        {published === undefined ? null : (
          // The whole hash, not a prefix: a truncated one cannot be compared
          // against anything, which is the only thing a hash is for.
          <Typography.Paragraph className="fact-note">
            <Typography.Text code>{published.content_hash}</Typography.Text>
          </Typography.Paragraph>
        )}
        <Typography.Paragraph type="secondary" className="fact-note">
          {t("draftComparisonUnavailable")}
        </Typography.Paragraph>
      </Card>
      {publishNote === null ? null : (
        <Alert className="page-alert" type="info" title={publishNote} showIcon />
      )}
      {saveError === null ? null : (
        <Alert
          className="page-alert"
          type={conflicted ? "warning" : "error"}
          title={saveError}
          showIcon
        />
      )}
      <Card title={t("draftSection")} variant="borderless">
        <Form<DraftValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          initialValues={valuesOf(draft.data)}
          onFinish={(values) => saveDraft.mutate(values)}
        >
          <Form.Item
            name="personality"
            label={t("personality")}
            rules={[
              { required: true, whitespace: true, message: t("required") },
              { max: 8192, message: t("personalityMaximum") },
            ]}
          >
            <Input.TextArea rows={6} />
          </Form.Item>
          <Form.Item name="provider" label={t("modelProvider")} rules={[{ required: true }]}>
            <Select
              options={[
                { value: "deterministic", label: t("modelProviderDeterministic") },
                { value: "openai_compatible", label: t("modelProviderEndpoint") },
              ]}
            />
          </Form.Item>
          {provider === "openai_compatible" ? (
            <Form.Item
              name="endpoint_id"
              label={t("modelEndpoint")}
              rules={[{ required: true, message: t("required") }]}
              // An empty dropdown reads as a loading bug. Saying that nothing is
              // registered points at the person who can change that.
              extra={endpoints.data?.length === 0 ? t("modelEndpointsEmpty") : undefined}
            >
              <Select
                options={(endpoints.data ?? [])
                  .filter((entry) => entry.status === "active")
                  .map((entry) => ({ value: entry.id, label: entry.name }))}
                loading={endpoints.isLoading}
              />
            </Form.Item>
          ) : (
            <Form.Item name="scenario" label={t("modelScenario")} rules={[{ required: true }]}>
              <Select
                options={MODEL_SCENARIOS.map((scenario) => ({ value: scenario, label: scenario }))}
              />
            </Form.Item>
          )}
          <Typography.Title level={5}>{t("limitsSection")}</Typography.Title>
          <div className="limit-grid">
            {LIMITS.map((limit) => (
              <Form.Item
                key={limit.name}
                name={limit.name}
                label={limit.label}
                rules={[{ required: true, message: t("required") }]}
              >
                <InputNumber min={limit.min} max={limit.max} className="full-width" />
              </Form.Item>
            ))}
          </div>
          <Typography.Title level={5}>{t("toolsSection")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("toolsPhaseGap")}</Typography.Paragraph>
          <Space>
            <Button type="primary" htmlType="submit" loading={saveDraft.isPending}>
              {t("saveDraft")}
            </Button>
            <Button onClick={() => void reload()} loading={draft.isFetching}>
              {t("reloadDraft")}
            </Button>
          </Space>
        </Form>
      </Card>
    </>
  );
}
