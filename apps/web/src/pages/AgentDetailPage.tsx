import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Typography,
} from "antd";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api, apiWithStatus } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  AgentDraftResponse,
  AgentResponse,
  AgentSpecDocument,
  AgentVersionDetailResponse,
  AgentVersionResponse,
  HttpToolResponse,
  HttpToolVersionResponse,
  McpServerResponse,
  McpServerVersionResponse,
  ModelEndpointSummary,
  OutboundScopeEntry,
  SkillResponse,
  SkillVersionResponse,
  WritePolicy,
} from "../api/types";
import { IMPLEMENTED_TOOLS, MODEL_SCENARIOS } from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
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
  tools: string[];
  delivery_enabled: boolean;
  sync_timeout_seconds: number;
  /** Bound skill *version* ids. Never names — see `agentSkillsHint`. */
  skills: string[];
  /** Targets this Agent may reach, chosen from what the workspace approved. */
  network: string[];
  /**
   * Bound HTTP operations as `versionId::operationId`, and MCP tools as
   * `versionId::toolName`.
   *
   * One flat list rather than a nested editor because that is what a
   * multi-select can hold, and the pair is split back apart on the way into
   * the spec. The version id is on the left of every value, so a binding can
   * never lose which document it was made against.
   */
  http_tools: string[];
  mcp_tools: string[];
  /** §16.3's choice, one per bound tool family. */
  http_write_policy: WritePolicy | undefined;
  mcp_write_policy: WritePolicy | undefined;
};

/** Splits `versionId::name` back into its two halves. */
function pairsOf(values: string[]): Map<string, string[]> {
  const grouped = new Map<string, string[]>();
  for (const value of values) {
    const [versionId = "", name = ""] = value.split("::");
    if (versionId === "" || name === "") {
      continue;
    }
    grouped.set(versionId, [...(grouped.get(versionId) ?? []), name]);
  }
  return grouped;
}

/** One bound summary's estimated cost, as a refused publish reports it. */
type SummaryCost = { skill: string; estimated_tokens: number };

/**
 * The per-summary estimates out of a refusal, or null for any other failure.
 *
 * Read defensively: this is server context, and a console that assumed its
 * shape would render `undefined tokens` on a payload it half understood.
 */
function summaryCostsOf(error: unknown): SummaryCost[] | null {
  if (!(error instanceof ApiError) || error.code !== "skill_summary_budget_exceeded") {
    return null;
  }
  const summaries = error.context.summaries;
  if (!Array.isArray(summaries)) {
    return null;
  }
  const costs: SummaryCost[] = [];
  for (const entry of summaries) {
    const item = entry as { skill?: unknown; estimated_tokens?: unknown };
    if (typeof item.skill === "string" && typeof item.estimated_tokens === "number") {
      costs.push({ skill: item.skill, estimated_tokens: item.estimated_tokens });
    }
  }
  return costs.length === 0 ? null : costs;
}

const WRITE_POLICIES: WritePolicy[] = ["disabled", "preauthorized", "governance"];

const WRITE_POLICY_LABELS: Record<WritePolicy, MessageKey> = {
  disabled: "writeDisabled",
  preauthorized: "writePreauthorized",
  governance: "writeGovernance",
};

type NameValues = {
  name: string;
  alias: string;
};

const DEFAULT_DELIVERY = { enabled: false, sync_timeout_seconds: 60 };

function valuesOf(draft: AgentDraftResponse): DraftValues {
  const policy = draft.spec.model_policy;
  const delivery = draft.spec.delivery ?? DEFAULT_DELIVERY;
  return {
    personality: draft.spec.personality,
    provider: policy.provider,
    scenario: policy.provider === "deterministic" ? policy.scenario : "complete",
    endpoint_id: policy.provider === "openai_compatible" ? policy.endpoint_id : undefined,
    ...draft.spec.limits,
    tools: [...draft.spec.tools],
    delivery_enabled: delivery.enabled,
    sync_timeout_seconds: delivery.sync_timeout_seconds,
    skills: (draft.spec.skills ?? []).map((binding) => binding.skill_version_id),
    network: [...(draft.spec.network?.allow ?? [])],
    http_tools: (draft.spec.http_tools ?? []).flatMap((binding) =>
      binding.operations.map((name) => `${binding.http_tool_version_id}::${name}`),
    ),
    mcp_tools: (draft.spec.mcp_tools ?? []).flatMap((binding) =>
      binding.tools.map((name) => `${binding.mcp_server_version_id}::${name}`),
    ),
    // One policy per family rather than per binding: the form offers one
    // choice, and every binding it writes carries it. An author who needs two
    // different answers publishes two Agents, which is the honest shape of
    // that requirement.
    http_write_policy: (draft.spec.http_tools ?? [])[0]?.write_policy ?? undefined,
    mcp_write_policy: (draft.spec.mcp_tools ?? [])[0]?.write_policy ?? undefined,
  };
}

function specOf(values: DraftValues): AgentSpecDocument {
  const spec: AgentSpecDocument = {
    schema_version: 1,
    personality: values.personality,
    model_policy:
      values.provider === "openai_compatible"
        ? { provider: "openai_compatible", endpoint_id: values.endpoint_id ?? "" }
        : { provider: "deterministic", scenario: values.scenario },
    tools: values.tools,
    limits: {
      max_execution_seconds: values.max_execution_seconds,
      max_elapsed_seconds: values.max_elapsed_seconds,
      max_model_calls: values.max_model_calls,
      max_tool_calls: values.max_tool_calls,
      max_derived_retries: values.max_derived_retries,
    },
  };
  if (values.network.length > 0) {
    // Left out entirely when nothing is chosen, so an Agent that never asked
    // for the network publishes the document it published before it could.
    spec.network = { allow: values.network };
  }
  if (values.skills.length > 0) {
    // Left out entirely when nothing is bound, so an Agent with no skills
    // publishes the same document it published before skills existed.
    spec.skills = values.skills.map((id) => ({ skill_version_id: id }));
  }
  // Same rule for both tool families, and for the same reason: an Agent that
  // binds none must publish the document it published before they existed.
  const http = pairsOf(values.http_tools);
  if (http.size > 0) {
    spec.http_tools = [...http].map(([versionId, operations]) => ({
      http_tool_version_id: versionId,
      operations,
      write_policy: values.http_write_policy ?? null,
    }));
  }
  const mcp = pairsOf(values.mcp_tools);
  if (mcp.size > 0) {
    spec.mcp_tools = [...mcp].map(([versionId, tools]) => ({
      mcp_server_version_id: versionId,
      tools,
      write_policy: values.mcp_write_policy ?? null,
    }));
  }
  const timeout = values.sync_timeout_seconds ?? DEFAULT_DELIVERY.sync_timeout_seconds;
  if (
    values.delivery_enabled !== DEFAULT_DELIVERY.enabled ||
    timeout !== DEFAULT_DELIVERY.sync_timeout_seconds
  ) {
    spec.delivery = {
      enabled: values.delivery_enabled,
      sync_timeout_seconds: timeout,
    };
  }
  return spec;
}

function summarizeSpec(spec: AgentSpecDocument): Record<string, string> {
  const delivery = spec.delivery ?? DEFAULT_DELIVERY;
  return {
    personality: spec.personality,
    model: JSON.stringify(spec.model_policy),
    tools: spec.tools.join(", ") || "—",
    skills: (spec.skills ?? []).map((binding) => binding.skill_version_id).join(", ") || "—",
    network: (spec.network?.allow ?? []).join(", ") || "—",
    http_tools:
      (spec.http_tools ?? []).flatMap((binding) => binding.operations).join(", ") || "—",
    mcp_tools: (spec.mcp_tools ?? []).flatMap((binding) => binding.tools).join(", ") || "—",
    max_execution_seconds: String(spec.limits.max_execution_seconds),
    max_elapsed_seconds: String(spec.limits.max_elapsed_seconds),
    max_model_calls: String(spec.limits.max_model_calls),
    max_tool_calls: String(spec.limits.max_tool_calls),
    max_derived_retries: String(spec.limits.max_derived_retries),
    delivery: delivery.enabled
      ? `chat_completions/${delivery.sync_timeout_seconds}s`
      : "off",
  };
}

export function AgentDetailPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const { agentId = "" } = useParams();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<DraftValues>();
  const [nameForm] = Form.useForm<NameValues>();
  const [modal, contextHolder] = Modal.useModal();
  const [saveError, setSaveError] = useState<string | null>(null);
  const [publishNote, setPublishNote] = useState<string | null>(null);
  // Per-summary estimates from a refused publish. Shown as themselves rather
  // than summed, so an author can see which description is the expensive one
  // instead of shortening all of them — the shape `context_budget_unsatisfied`
  // already uses for its per-segment advice.
  const [summaryCosts, setSummaryCosts] = useState<SummaryCost[] | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const enabled = workspaceId !== null && agentId !== "";
  const limits: { name: keyof DraftValues; label: MessageKey; min: number; max: number }[] = [
    { name: "max_execution_seconds", label: "maxExecutionSeconds", min: 1, max: 900 },
    { name: "max_elapsed_seconds", label: "maxElapsedSeconds", min: 60, max: 86_400 },
    { name: "max_model_calls", label: "maxModelCalls", min: 1, max: 20 },
    { name: "max_tool_calls", label: "maxToolCalls", min: 0, max: 50 },
    { name: "max_derived_retries", label: "maxDerivedRetries", min: 0, max: 3 },
  ];

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
  const endpoints = useQuery({
    queryKey: ["model-endpoints"] as const,
    queryFn: () => api<ModelEndpointSummary[]>("/api/v1/model-endpoints", scope),
  });
  // Every skill this workspace can see, with its versions, so the picker can
  // offer "name v2" rather than a bare uuid. Read here rather than on the
  // Skills page's cache: a binding made against a stale list is a binding to a
  // version that may since have been withdrawn, and publishing would refuse it
  // with a message about a version nobody remembers choosing.
  const skills = useQuery({
    queryKey: ["skills", workspaceId] as const,
    queryFn: () => api<SkillResponse[]>("/api/v1/skills", scope),
  });
  const skillVersions = useQuery({
    queryKey: ["skill-version-options", workspaceId, (skills.data ?? []).length] as const,
    enabled: (skills.data ?? []).length > 0,
    queryFn: async () => {
      const lists = await Promise.all(
        (skills.data ?? []).map(async (skill) => ({
          skill,
          versions: await api<SkillVersionResponse[]>(
            `/api/v1/skills/${skill.id}/versions`,
            scope,
          ),
        })),
      );
      return lists;
    },
  });
  // The two tool catalogs, read for the same reason the skills are: a binding
  // made against a stale list is a binding to a version that may since have
  // been withdrawn, and publishing would refuse it with a message about a
  // version nobody remembers choosing.
  const httpTools = useQuery({
    queryKey: ["http-tools", workspaceId] as const,
    queryFn: () => api<HttpToolResponse[]>("/api/v1/http-tools", scope),
  });
  const httpVersions = useQuery({
    queryKey: ["http-tool-options", workspaceId, (httpTools.data ?? []).length] as const,
    enabled: (httpTools.data ?? []).length > 0,
    queryFn: async () =>
      Promise.all(
        (httpTools.data ?? []).map(async (tool) => ({
          tool,
          versions: await api<HttpToolVersionResponse[]>(
            `/api/v1/http-tools/${tool.id}/versions`,
            scope,
          ),
        })),
      ),
  });
  const mcpServers = useQuery({
    queryKey: ["mcp-servers", workspaceId] as const,
    queryFn: () => api<McpServerResponse[]>("/api/v1/mcp-servers", scope),
  });
  const mcpVersions = useQuery({
    queryKey: ["mcp-options", workspaceId, (mcpServers.data ?? []).length] as const,
    enabled: (mcpServers.data ?? []).length > 0,
    queryFn: async () =>
      Promise.all(
        (mcpServers.data ?? []).map(async (server) => ({
          server,
          versions: await api<McpServerVersionResponse[]>(
            `/api/v1/mcp-servers/${server.id}/versions`,
            scope,
          ),
        })),
      ),
  });
  // What this workspace approved, which is exactly the list of choices an
  // author has. Offered rather than typed: an entry outside it is refused at
  // publish, and a field that lets somebody write one is a field that teaches
  // them to publish and see.
  const outbound = useQuery({
    queryKey: ["outbound-scopes", "workspace", workspaceId] as const,
    queryFn: () => api<OutboundScopeEntry[]>("/api/v1/outbound-scopes/workspace", scope),
  });
  const provider = Form.useWatch("provider", form);
  const deliveryEnabled = Form.useWatch("delivery_enabled", form);
  const watched = Form.useWatch([], form) as DraftValues | undefined;
  const agentQuery = ["agent", workspaceId, agentId] as const;
  const versionsQuery = ["agent-versions", workspaceId, agentId] as const;
  const versions = useQuery({
    queryKey: versionsQuery,
    queryFn: () => api<AgentVersionResponse[]>(`/api/v1/agents/${agentId}/versions`, scope),
    enabled,
  });
  const publishedId = agent.data?.current_version_id ?? null;
  const published = useQuery({
    queryKey: ["agent-version", workspaceId, agentId, publishedId] as const,
    queryFn: () =>
      api<AgentVersionDetailResponse>(
        `/api/v1/agents/${agentId}/versions/${publishedId ?? ""}`,
        scope,
      ),
    enabled: enabled && publishedId !== null,
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
    onError: (caught) => setSaveError(problemMessage(caught)),
  });

  const rename = useMutation({
    mutationFn: (values: NameValues) =>
      api<AgentResponse>(`/api/v1/agents/${agentId}`, {
        ...scope,
        method: "PATCH",
        body: JSON.stringify(values),
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(agentQuery, updated);
      setSaveError(null);
    },
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
      setPublishNote(status === 200 ? t("publishUnchanged") : null);
      setSaveError(null);
      setSummaryCosts(null);
    },
    onError: (caught) => {
      setSaveError(problemMessage(caught));
      setSummaryCosts(summaryCostsOf(caught));
    },
  });

  const rollback = useMutation({
    mutationFn: (versionId: string) =>
      api<AgentVersionResponse>(`/api/v1/agents/${agentId}/rollback`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ version_id: versionId }),
      }),
    onSuccess: (version) => {
      queryClient.setQueryData<AgentResponse>(agentQuery, (current) =>
        current === undefined
          ? current
          : { ...current, status: "published", current_version_id: version.id },
      );
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
      onOk: () => publish.mutateAsync(revision).catch(() => undefined),
    });
  }

  function askToRollback(versionId: string, number: number): void {
    void modal.confirm({
      title: `${t("rollback")} v${number}`,
      content: t("rollbackWarning"),
      okText: t("confirm"),
      cancelText: t("cancel"),
      onOk: () => rollback.mutateAsync(versionId).catch(() => undefined),
    });
  }

  const currentVersion = versions.data?.find((version) => version.id === agent.data?.current_version_id);
  const lastError = saveDraft.error ?? publish.error ?? rename.error ?? rollback.error;
  const conflicted =
    lastError instanceof ApiError && lastError.code === "draft_revision_conflict";
  const draftValues =
    watched !== undefined && watched.personality !== undefined
      ? watched
      : valuesOf(draft.data);
  const draftSummary = summarizeSpec(specOf(draftValues));
  const publishedSummary =
    published.data === undefined ? null : summarizeSpec(published.data.spec);
  const diffEntries =
    publishedSummary === null
      ? []
      : Object.entries(draftSummary).filter(([key, value]) => publishedSummary[key] !== value);

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{agent.data.name}</Typography.Title>
          <Typography.Paragraph type="secondary">{agent.data.alias}</Typography.Paragraph>
        </div>
        <Space wrap>
          <Link to={`/workspaces/${workspaceId}/agents/${agentId}/playground`}>
            <Button>{t("openPlayground")}</Button>
          </Link>
          <Button
            type="primary"
            loading={publish.isPending}
            onClick={() => askToPublish(draft.data?.revision ?? 0)}
          >
            {t("publish")}
          </Button>
        </Space>
      </div>
      <Card title={t("renameAgent")} variant="borderless" className="page-alert">
        <Form<NameValues>
          form={nameForm}
          layout="inline"
          requiredMark={false}
          initialValues={{ name: agent.data.name, alias: agent.data.alias }}
          onFinish={(values) => rename.mutate(values)}
        >
          <Form.Item
            name="name"
            label={t("agentName")}
            rules={[
              { required: true, whitespace: true, message: t("required") },
              { max: 120, message: t("agentNameMaximum") },
            ]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="alias"
            label={t("agentAlias")}
            extra={t("agentAliasHint")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item>
            <Button htmlType="submit" loading={rename.isPending}>
              {t("saveName")}
            </Button>
          </Form.Item>
        </Form>
      </Card>
      <Card variant="borderless" className="page-alert">
        <Space size="large" wrap>
          <Typography.Text strong>
            {`${t("draftRevision")} ${draft.data.revision}`}
          </Typography.Text>
          {agent.data.current_version_id === null ? (
            <Typography.Text>{t("agentUnpublished")}</Typography.Text>
          ) : (
            <Typography.Text>
              {`${t("currentVersion")} v${currentVersion?.version_number ?? "?"}`}
            </Typography.Text>
          )}
        </Space>
        {currentVersion === undefined ? null : (
          <Typography.Paragraph className="fact-note">
            <Typography.Text code>{currentVersion.content_hash}</Typography.Text>
          </Typography.Paragraph>
        )}
        <Typography.Title level={5}>{t("diffSection")}</Typography.Title>
        {publishedSummary === null ? (
          <Typography.Paragraph type="secondary">{t("diffUnpublished")}</Typography.Paragraph>
        ) : diffEntries.length === 0 ? (
          <Typography.Paragraph type="secondary">{t("diffNone")}</Typography.Paragraph>
        ) : (
          <ul>
            {diffEntries.map(([key, value]) => (
              <li key={key}>
                <Typography.Text>{key}</Typography.Text>
                {": "}
                <Typography.Text delete>{publishedSummary[key]}</Typography.Text>
                {" → "}
                <Typography.Text>{value}</Typography.Text>
              </li>
            ))}
          </ul>
        )}
      </Card>
      {publishNote === null ? null : (
        <Alert className="page-alert" type="info" title={publishNote} showIcon />
      )}
      {saveError === null ? null : (
        <Alert
          className="page-alert"
          type={conflicted ? "warning" : "error"}
          title={saveError}
          description={
            summaryCosts === null ? undefined : (
              <>
                <Typography.Paragraph>{t("skillSummaryBudgetExceeded")}</Typography.Paragraph>
                <ul>
                  {summaryCosts.map((cost) => (
                    <li key={cost.skill}>
                      {t("skillSummaryEstimate")
                        .replace("{name}", cost.skill)
                        .replace("{tokens}", String(cost.estimated_tokens))}
                    </li>
                  ))}
                </ul>
              </>
            )
          }
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
              {/*
                Searchable, and that is a fix rather than a nicety. The list is
                long enough that rc-select virtualizes it, and an option below
                the fold cannot be reliably clicked — the element under the
                cursor is recycled mid-click and detaches. M2D's record filed
                that as an open defect and worked around it by publishing
                through the API; this is the change it said was needed. Typing
                filters the list down to a few rows, so nothing is ever
                selected out of a scrolling viewport.
              */}
              <Select
                showSearch
                optionFilterProp="value"
                options={MODEL_SCENARIOS.map((scenario) => ({ value: scenario, label: scenario }))}
              />
            </Form.Item>
          )}
          <Typography.Title level={5}>{t("limitsSection")}</Typography.Title>
          <div className="limit-grid">
            {limits.map((limit) => (
              <Form.Item
                key={limit.name}
                name={limit.name}
                label={t(limit.label)}
                rules={[{ required: true, message: t("required") }]}
              >
                <InputNumber min={limit.min} max={limit.max} className="full-width" />
              </Form.Item>
            ))}
          </div>
          <Typography.Title level={5}>{t("toolsSection")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("toolsHint")}</Typography.Paragraph>
          <Form.Item name="tools">
            <Checkbox.Group
              options={IMPLEMENTED_TOOLS.map((name) => ({ value: name, label: name }))}
            />
          </Form.Item>
          <Typography.Title level={5}>{t("agentSkills")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("agentSkillsHint")}</Typography.Paragraph>
          <Form.Item name="skills" label={t("agentSkillPick")}>
            <Select
              mode="multiple"
              allowClear
              loading={skills.isLoading || skillVersions.isLoading}
              placeholder={t("agentSkillsEmpty")}
              // Grouped by skill, so choosing is "which skill, then which
              // version of it" even though what is stored is one version id.
              options={(skillVersions.data ?? []).map((entry) => ({
                label: entry.skill.name,
                options: entry.versions
                  .filter((version) => version.bindable)
                  .map((version) => ({
                    value: version.id,
                    label: `${entry.skill.name} v${String(version.version_number)}`,
                  })),
              }))}
            />
          </Form.Item>
          <Typography.Title level={5}>{t("agentHttpTools")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("agentHttpToolsHint")}</Typography.Paragraph>
          <Form.Item name="http_tools" label={t("agentHttpTools")}>
            <Select
              mode="multiple"
              allowClear
              loading={httpTools.isLoading || httpVersions.isLoading}
              placeholder={t("emptyHttpTools")}
              // Grouped by tool and version, and the label carries whether the
              // operation writes: what an author needs while choosing is
              // whether this one will stop for a person.
              options={(httpVersions.data ?? []).flatMap((entry) =>
                entry.versions
                  .filter((version) => version.bindable)
                  .map((version) => ({
                    label: `${entry.tool.name} v${String(version.version_number)}`,
                    options: version.operations.map((operation) => ({
                      value: `${version.id}::${operation.operation_id}`,
                      label: operation.read_only
                        ? `${operation.method} ${operation.operation_id}`
                        : `${operation.method} ${operation.operation_id} · ${t("httpToolWrites")}`,
                    })),
                  })),
              )}
            />
          </Form.Item>
          <Form.Item
            name="http_write_policy"
            label={t("agentHttpWritePolicy")}
            extra={t("agentWritePolicyHint")}
          >
            <Select allowClear options={WRITE_POLICIES.map((value) => ({
              value,
              label: t(WRITE_POLICY_LABELS[value]),
            }))} />
          </Form.Item>
          <Typography.Title level={5}>{t("agentMcpTools")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("agentMcpHint")}</Typography.Paragraph>
          <Form.Item name="mcp_tools" label={t("agentMcpTools")}>
            <Select
              mode="multiple"
              allowClear
              loading={mcpServers.isLoading || mcpVersions.isLoading}
              placeholder={t("emptyMcpServers")}
              options={(mcpVersions.data ?? []).flatMap((entry) =>
                entry.versions
                  .filter((version) => version.bindable)
                  .map((version) => ({
                    label: `${entry.server.name} v${String(version.version_number)}`,
                    options: version.tools.map((tool) => ({
                      value: `${version.id}::${tool.name}`,
                      label: tool.name,
                    })),
                  })),
              )}
            />
          </Form.Item>
          <Form.Item
            name="mcp_write_policy"
            label={t("agentMcpWritePolicy")}
            extra={t("agentWritePolicyHint")}
          >
            <Select allowClear options={WRITE_POLICIES.map((value) => ({
              value,
              label: t(WRITE_POLICY_LABELS[value]),
            }))} />
          </Form.Item>
          <Typography.Title level={5}>{t("agentNetwork")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("agentNetworkHint")}</Typography.Paragraph>
          <Form.Item name="network" label={t("agentNetwork")}>
            <Select
              mode="multiple"
              allowClear
              loading={outbound.isLoading}
              placeholder={t("agentNetworkEmpty")}
              options={(outbound.data ?? []).map((item) => ({
                value: item.entry,
                label: item.entry,
              }))}
            />
          </Form.Item>
          <Typography.Title level={5}>{t("deliverySection")}</Typography.Title>
          <Form.Item name="delivery_enabled" label={t("chatCompletionsEnabled")} valuePropName="checked">
            <Switch />
          </Form.Item>
          {deliveryEnabled ? (
            <Form.Item
              name="sync_timeout_seconds"
              label={t("syncTimeoutSeconds")}
              rules={[{ required: true, message: t("required") }]}
            >
              <InputNumber min={1} max={60} className="full-width" />
            </Form.Item>
          ) : null}
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
      {(versions.data ?? []).length === 0 ? null : (
        <Card title={t("currentVersion")} variant="borderless" className="page-alert">
          {(versions.data ?? []).map((version) => (
            <Space key={version.id} className="workspace-row" wrap>
              <Typography.Text>{`v${version.version_number}`}</Typography.Text>
              <Typography.Text code>{version.content_hash}</Typography.Text>
              {version.id === agent.data.current_version_id ? (
                <Typography.Text type="secondary">{t("agentPublished")}</Typography.Text>
              ) : (
                <Button
                  loading={rollback.isPending}
                  onClick={() => askToRollback(version.id, version.version_number)}
                >
                  {t("rollback")}
                </Button>
              )}
            </Space>
          ))}
        </Card>
      )}
    </>
  );
}
