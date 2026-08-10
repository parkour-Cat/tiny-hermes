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

import { ApiError, api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  AgentDraftResponse,
  AgentResponse,
  AgentSpecDocument,
  AgentVersionResponse,
} from "../api/types";
import { MODEL_SCENARIOS } from "../api/types";
import { t } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type DraftValues = {
  personality: string;
  scenario: string;
  max_execution_seconds: number;
  max_elapsed_seconds: number;
  max_model_calls: number;
  max_tool_calls: number;
  max_derived_retries: number;
};

function valuesOf(draft: AgentDraftResponse): DraftValues {
  return {
    personality: draft.spec.personality,
    scenario: draft.spec.model_policy.scenario,
    ...draft.spec.limits,
  };
}

function specOf(values: DraftValues): AgentSpecDocument {
  return {
    schema_version: 1,
    personality: values.personality,
    // The only provider phase two has. Naming it here rather than echoing the
    // loaded spec keeps the console from carrying forward a value it could not
    // have produced.
    model_policy: { provider: "deterministic", scenario: values.scenario },
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
  const scope = { workspace: workspaceId ?? "" };
  const enabled = workspaceId !== null && agentId !== "";

  const agent = useQuery({
    queryKey: ["agent", workspaceId, agentId],
    queryFn: () => api<AgentResponse>(`/api/v1/agents/${agentId}`, scope),
    enabled,
  });
  const draftQuery = ["agent-draft", workspaceId, agentId] as const;
  const draft = useQuery({
    queryKey: draftQuery,
    queryFn: () => api<AgentDraftResponse>(`/api/v1/agents/${agentId}/draft`, scope),
    enabled,
  });
  const versions = useQuery({
    queryKey: ["agent-versions", workspaceId, agentId],
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

  const published = versions.data?.find((version) => version.id === agent.data?.current_version_id);
  const conflicted = saveDraft.error instanceof ApiError
    && saveDraft.error.code === "draft_revision_conflict";

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{agent.data.name}</Typography.Title>
          <Typography.Paragraph type="secondary">{agent.data.alias}</Typography.Paragraph>
        </div>
      </div>
      <Card variant="borderless" className="page-alert">
        {/* Two facts, side by side and unrelated. Stated rather than compared:
            the draft endpoint returns no content hash, so "unchanged since
            publish" is not something this console can know. */}
        <Space size="large" wrap>
          <Typography.Text strong>
            {`${t("draftRevision")} ${draft.data.revision}`}
          </Typography.Text>
          <Typography.Text>
            {agent.data.current_version_id === null
              ? t("agentUnpublished")
              : `${t("currentVersion")} v${published?.version_number ?? "?"} · ${
                  published?.content_hash.slice(0, 12) ?? ""
                }`}
          </Typography.Text>
        </Space>
        <Typography.Paragraph type="secondary" className="fact-note">
          {t("draftComparisonUnavailable")}
        </Typography.Paragraph>
      </Card>
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
          <Form.Item name="scenario" label={t("modelScenario")} rules={[{ required: true }]}>
            <Select
              options={MODEL_SCENARIOS.map((scenario) => ({ value: scenario, label: scenario }))}
            />
          </Form.Item>
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
