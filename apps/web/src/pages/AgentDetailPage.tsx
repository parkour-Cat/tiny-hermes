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
import { UnsavedChangesGuard } from "../forms/UnsavedChangesGuard";
import { specDiff } from "./specDiff";
import { SharedMemoryButton } from "./SharedMemoryButton";
import { FormSection } from "../forms/FormSection";
import { PageHeading } from "../ui/PageHeading";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useWorkspacePermissions } from "../workspace/WorkspacePermissions";
import { useAuth } from "../auth/AuthProvider";
import { agentDraftKey, readAgentDraft, writeAgentDraft } from "./agentDraftStorage";
import type { LocalAgentDraft } from "./agentDraftStorage";

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
  end_user_access_enabled: boolean;
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

/** 一张表单：名称与别名跟人格并排在「身份」里，但保存时各走各的请求——
 *  名称不是版本化 spec 的一部分，改名不该长出一个草稿修订。 */
export type FormValues = DraftValues & NameValues;

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
    end_user_access_enabled: draft.spec.end_user_access?.enabled ?? false,
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
  if (values.end_user_access_enabled) {
    // Written only when open, and that asymmetry is the point: `models.py`'s
    // normalization pops this key when it is `None`, so an Agent that never
    // opened the end-user entry point publishes the document it published
    // before this field existed. A `{ enabled: false }` would keep the key
    // and move every one of those Agents' content hashes.
    spec.end_user_access = { enabled: true };
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

/** How many distinct documents a `versionId::name` list binds. */
function documentsIn(bindings: string[]): number {
  return new Set(bindings.map((entry) => entry.split("::")[0])).size;
}

/** 「能力」段折叠时那一行。`t()` 不支持插值，所以数字在这里拼，文案里只有
 *  固定词。 */
function capabilitySummary(t: (key: MessageKey) => string, values: DraftValues): string {
  const mcp = documentsIn(values.mcp_tools);
  return [
    `${t("agentSummaryBound")} ${values.skills.length} ${t("agentSummarySkillsUnit")}`,
    `${values.tools.length} ${t("agentSummaryToolsUnit")}`,
    `${documentsIn(values.http_tools)} ${t("agentSummaryHttpUnit")}`,
    mcp === 0 ? t("agentSummaryNoMcp") : `${mcp} ${t("agentSummaryMcpUnit")}`,
    values.network.length > 0 ? t("agentSummaryNetworkOn") : t("agentSummaryNetworkOff"),
  ].join(" · ");
}

function exposureSummary(t: (key: MessageKey) => string, values: DraftValues): string {
  return [
    values.end_user_access_enabled ? t("agentSummaryEndUserOn") : t("agentSummaryEndUserOff"),
    values.delivery_enabled ? t("agentSummaryDeliveryOn") : t("agentSummaryDeliveryOff"),
  ].join(" · ");
}

export function AgentDetailPage() {
  const auth = useAuth();
  const { writer } = useWorkspacePermissions();
  const workspaceId = useWorkspaceId();
  const { agentId = "" } = useParams();
  if (auth.loading || auth.error || !auth.user) return <Card loading variant="borderless" />;
  const key = agentDraftKey(auth.user.id, workspaceId ?? "", agentId);
  return <AgentEditor key={`${key}:${writer}`} storageKey={key} />;
}

function AgentEditor({ storageKey }: { storageKey: string }) {
  const { writer } = useWorkspacePermissions();
  const t = useT();
  const workspaceId = useWorkspaceId();
  const { agentId = "" } = useParams();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<FormValues>();
  const [modal, contextHolder] = Modal.useModal();
  const [saveError, setSaveError] = useState<string | null>(null);
  const [publishNote, setPublishNote] = useState<string | null>(null);
  const [recovery] = useState(() => writer ? readAgentDraft(storageKey) : { draft: null, failed: false });
  const [localDraft, setLocalDraft] = useState<LocalAgentDraft | null>(recovery.draft);
  const [storageFailed, setStorageFailed] = useState(recovery.failed);
  const [restored] = useState(localDraft !== null);
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
    // Model and tool calls stop at the highest ceiling the backend's settings
    // allow, not at the ceiling in force: that is an administrator's setting
    // this page cannot see, and the server refuses an over-ceiling draft with
    // both numbers. A lower `max` here clamped 120 to 50 without saying so.
    { name: "max_model_calls", label: "maxModelCalls", min: 1, max: 200 },
    { name: "max_tool_calls", label: "maxToolCalls", min: 0, max: 5000 },
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
  const watched = Form.useWatch([], form) as FormValues | undefined;
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

  function rememberEdits(values: FormValues, baseDraft: AgentDraftResponse, baseAgent: AgentResponse): void {
    const unchanged = JSON.stringify(specOf(values)) === JSON.stringify(specOf(valuesOf(baseDraft))) &&
      values.name === baseAgent.name && values.alias === baseAgent.alias;
    const next = unchanged ? null : { revision: baseDraft.revision, name: baseAgent.name, alias: baseAgent.alias, values };
    setLocalDraft(next);
    setStorageFailed(!writeAgentDraft(storageKey, next));
  }

  const saveDraft = useMutation({
    mutationFn: ({ values, revision }: { values: DraftValues; revision: number }) =>
      api<AgentDraftResponse>(`/api/v1/agents/${agentId}/draft`, {
        ...scope,
        method: "PUT",
        body: JSON.stringify({
          expected_revision: revision,
          spec: specOf(values),
        }),
      }),
    onSuccess: (saved) => {
      if (agent.data) rememberEdits(form.getFieldsValue(true), saved, agent.data);
      queryClient.setQueryData(draftQuery, saved);
      setSaveError(null);
    },
    onError: (caught) => setSaveError(problemMessage(caught, t)),
  });

  const rename = useMutation({
    mutationFn: (values: NameValues) =>
      api<AgentResponse>(`/api/v1/agents/${agentId}`, {
        ...scope,
        method: "PATCH",
        body: JSON.stringify(values),
      }),
    onSuccess: (updated) => {
      if (draft.data) rememberEdits(form.getFieldsValue(true), draft.data, updated);
      queryClient.setQueryData(agentQuery, updated);
      setSaveError(null);
    },
    onError: (caught) => setSaveError(problemMessage(caught, t)),
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
      setSaveError(problemMessage(caught, t));
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
    onError: (caught) => setSaveError(problemMessage(caught, t)),
  });

  const failed = [agent, draft, versions].find((query) => query.isError);
  if (failed !== undefined) {
    return (
      <Alert
        type="error"
        title={problemMessage(failed.error, t)}
        action={<Button onClick={() => void failed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }
  if (agent.data === undefined || draft.data === undefined || !agent.isFetchedAfterMount || !draft.isFetchedAfterMount) {
    return <Card loading variant="borderless" />;
  }
  const loadedAgent = agent.data;
  const loadedDraft = draft.data;
  const canRestore = writer && localDraft?.revision === loadedDraft.revision &&
    localDraft.name === loadedAgent.name && localDraft.alias === loadedAgent.alias;
  const recoveryConflict = writer && localDraft !== null && !canRestore;

  function reload(): void {
    void modal.confirm({
      title: t("reloadDraft"),
      content: t("reloadDraftWarning"),
      okText: t("confirm"),
      cancelText: t("cancel"),
      onOk: async () => {
        const [fresh, freshAgent] = await Promise.all([draft.refetch(), agent.refetch()]);
        if (fresh.isSuccess && freshAgent.isSuccess) {
          form.setFieldsValue({ ...valuesOf(fresh.data), name: freshAgent.data.name, alias: freshAgent.data.alias });
          setLocalDraft(null);
          setStorageFailed(!writeAgentDraft(storageKey, null));
          setSaveError(null);
        }
      },
    });
  }

  function askToPublish(revision: number): void {
    void modal.confirm({
      title: t("publish"),
      content: dirty ? t("publishSaveWarning") : `${t("publishWarningPrefix")}${revision}${t("publishWarningSuffix")}`,
      okText: t("confirm"),
      cancelText: t("cancel"),
      onOk: async () => {
        try {
          const values = await form.validateFields();
          if (values.name !== loadedAgent.name || values.alias !== loadedAgent.alias) {
            await rename.mutateAsync({ name: values.name, alias: values.alias });
          }
          const changed = JSON.stringify(specOf(values)) !== JSON.stringify(specOf(valuesOf(loadedDraft)));
          const savedRevision = changed
            ? (await saveDraft.mutateAsync({ values, revision })).revision
            : revision;
          await publish.mutateAsync(savedRevision);
        } catch {
          // Mutation errors are shown beside the retained draft; validation
          // errors remain on the fields. Neither path may publish old content.
        }
      },
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
  const dirty = JSON.stringify(specOf(draftValues)) !== JSON.stringify(specOf(valuesOf(loadedDraft))) ||
    (watched?.name !== undefined && watched.name !== loadedAgent.name) ||
    (watched?.alias !== undefined && watched.alias !== loadedAgent.alias);
  const saving = saveDraft.isPending || rename.isPending || publish.isPending;
  const diffEntries = published.data ? specDiff(published.data.spec, specOf(draftValues)) : [];
  const fieldLabels: Record<string, MessageKey> = {
    name: "agentName", alias: "agentAlias",
    personality: "personality", model_policy: "modelEndpoints", endpoint_id: "modelEndpoint",
    tools: "toolsSection", skills: "skills", http_tools: "httpTools", mcp_tools: "mcpServers",
    network: "agentNetwork", limits: "budgetSection", delivery: "deliverySection", end_user_access: "agentSummaryEndUserOn",
    max_execution_seconds: "maxExecutionSeconds", max_elapsed_seconds: "maxElapsedSeconds",
    max_model_calls: "maxModelCalls", max_tool_calls: "maxToolCalls", max_derived_retries: "maxDerivedRetries",
    temperature: "diffTemperature", max_output_tokens: "endpointMaxOutput", write_policy: "agentWritePolicy",
    http_tool_version_id: "currentVersion", mcp_server_version_id: "currentVersion", skill_version_id: "currentVersion",
    enabled: "diffEnabled", operations: "diffOperations", allow: "diffAllowed", sync_timeout_seconds: "syncTimeoutSeconds",
    provider: "modelProvider", scenario: "modelScenario",
  };
  const localDiff = localDraft === null ? [] : [
    ...specDiff(specOf(valuesOf(loadedDraft)), specOf(localDraft.values)),
    ...(["name", "alias"] as const).filter((field) => loadedAgent[field] !== localDraft.values[field])
      .map((field) => ({ path: field, before: JSON.stringify(loadedAgent[field]), after: JSON.stringify(localDraft.values[field]) })),
  ];

  return (
    <>
      {contextHolder}
      <UnsavedChangesGuard dirty={dirty || recoveryConflict} onDiscard={() => {
        const cleared = writeAgentDraft(storageKey, null);
        setStorageFailed(!cleared);
        if (cleared) setLocalDraft(null);
        return cleared;
      }} />
      {restored && canRestore && <Alert className="page-alert" type="info" showIcon title={t("agentEditsRestored")} />}
      {storageFailed && <Alert className="page-alert" type="warning" showIcon title={t("agentEditsStorageFailed")} />}
      {recoveryConflict && <Alert className="page-alert" type="warning" showIcon title={t("agentEditsConflict")}
        description={<details><summary>{t("agentEditsDifferences")}</summary><ul>
          {localDiff.map(({ path, before, after }) => <li key={path}>
            <Typography.Text strong>{path.split(".").map((part) => fieldLabels[part] ? t(fieldLabels[part]) : part).join(" · ")}</Typography.Text>
            <div>{t("agentEditsServer")}:</div>
            <Typography.Paragraph>{before.startsWith('"') ? String(JSON.parse(before)) : before}</Typography.Paragraph>
            <div>{t("agentEditsLocal")}:</div>
            <Typography.Paragraph copyable>{after.startsWith('"') ? String(JSON.parse(after)) : after}</Typography.Paragraph>
          </li>)}
        </ul></details>} />}
      <PageHeading
        kicker={t("agents")}
        title={agent.data.name}
        intro={agent.data.alias}
        extra={
          <Space wrap>
          <SharedMemoryButton key={agent.data.id} agentId={agent.data.id} agentName={agent.data.name} />
          <Link to={`/workspaces/${workspaceId}/agents/${agentId}/playground`}>
            <Button>{t("openPlayground")}</Button>
          </Link>

          </Space>
        }
      />
      <Space wrap className="editor-actions page-alert">
        <Button type="primary" loading={saving} disabled={!writer || recoveryConflict} onClick={() => form.submit()}>{t("saveDraft")}</Button>
        <Button loading={saving} disabled={!writer || recoveryConflict} onClick={() => askToPublish(localDraft?.revision ?? loadedDraft.revision)}>{t("publish")}</Button>
        <Button loading={draft.isFetching} onClick={reload}>{t("reloadDraft")}</Button>
        {dirty && <Typography.Text type="warning">{t("draftUnsaved")}</Typography.Text>}
      </Space>
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
        <Typography.Title level={5}>{t("diffSection")}</Typography.Title>
        {published.data === undefined ? (
          <Typography.Paragraph type="secondary">{t("diffUnpublished")}</Typography.Paragraph>
        ) : diffEntries.length === 0 ? (
          <Typography.Paragraph type="secondary">{t("diffNone")}</Typography.Paragraph>
        ) : (
          <ul>
            {diffEntries.map(({ path, before, after }) => (
              <li key={path} className="spec-change">
                <Typography.Text strong>{path.split(".").map((part) => fieldLabels[part] ? t(fieldLabels[part]) : /^\d+$/.test(part) ? `#${Number(part) + 1}` : part).join(" · ")}</Typography.Text>
                <div><Typography.Text delete>{before.startsWith('"') ? String(JSON.parse(before)) : before}</Typography.Text>{" → "}<Typography.Text>{after.startsWith('"') ? String(JSON.parse(after)) : after}</Typography.Text></div>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <details className="page-alert"><summary>{t("diffTechnical")}</summary><pre className="skill-file-body">{JSON.stringify({ published: published.data?.spec ?? null, draft: specOf(draftValues), content_hash: currentVersion?.content_hash ?? null }, null, 2)}</pre></details>
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
        <Form<FormValues>
          form={form}
          disabled={saving || !writer || recoveryConflict}
          layout="vertical"
          requiredMark={false}
          initialValues={canRestore ? localDraft.values : { ...valuesOf(loadedDraft), name: loadedAgent.name, alias: loadedAgent.alias }}
          onValuesChange={() => {
            // Conditional fields still have meaningful defaults in the form store.
            const values = form.getFieldsValue(true) as FormValues;
            const next = { revision: localDraft?.revision ?? loadedDraft.revision, name: localDraft?.name ?? loadedAgent.name, alias: localDraft?.alias ?? loadedAgent.alias, values };
            setLocalDraft(next);
            setStorageFailed(!writeAgentDraft(storageKey, next));
          }}
          // `specOf` reads the draft fields by name, so the two name fields
          // sharing this form never reach the spec.
          onFinish={(values) => { if (!recoveryConflict) saveDraft.mutate({ values, revision: localDraft?.revision ?? loadedDraft.revision }); }}
        >
          <FormSection
            title={t("agentSectionIdentity")}
            summary=""
            fields={["name", "alias", "personality"]}
            collapsible={false}
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
          {/* Its own button, not the draft's: the name is not part of the
              versioned document, and saving it must not mint a draft revision.
              One form because they belong to one 身份; two requests because
              they are two resources. */}
          <Form.Item>
            <Button
              loading={rename.isPending}
              onClick={() =>
                void form.validateFields(["name", "alias"]).then(({ name, alias }) =>
                  rename.mutate({ name, alias }),
                )
              }
            >
              {t("saveName")}
            </Button>
          </Form.Item>
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
          </FormSection>
          <FormSection
            title={t("agentSectionModel")}
            summary=""
            fields={[provider === "openai_compatible" ? "endpoint_id" : "scenario", ...limits.map((limit) => limit.name)]}
            collapsible={false}
          >
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
          </FormSection>
          {/* 能力：全是可选的，所以可以折叠——折叠条上说的是绑了什么，不是「能力」
              两个字。 */}
          <FormSection
            title={t("agentSectionCapability")}
            summary={capabilitySummary(t, draftValues)}
            fields={[]}
            collapsible
          >
          <Space wrap className="page-alert"><Link target="_blank" rel="noopener noreferrer" to={`/workspaces/${workspaceId}/tooling#skills`}>{t("skills")}</Link><Link target="_blank" rel="noopener noreferrer" to={`/workspaces/${workspaceId}/tooling#http-tools`}>{t("httpTools")}</Link><Link target="_blank" rel="noopener noreferrer" to={`/workspaces/${workspaceId}/tooling#mcp-servers`}>{t("mcpServers")}</Link></Space>
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
              optionFilterProp="label"
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
              optionFilterProp="label"
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
              optionFilterProp="label"
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
          </FormSection>
          <FormSection
            title={t("agentSectionExposure")}
            summary={exposureSummary(t, draftValues)}
            fields={deliveryEnabled ? ["sync_timeout_seconds"] : []}
            collapsible
          >
          <Typography.Title level={5}>{t("endUserSection")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("endUserAccessIntro")}</Typography.Paragraph>
          <Form.Item
            name="end_user_access_enabled"
            label={t("endUserAccessEnabled")}
            valuePropName="checked"
          >
            <Switch />
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
          </FormSection>

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
                  disabled={!writer}
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
