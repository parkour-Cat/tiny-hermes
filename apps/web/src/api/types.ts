/**
 * The committed response shapes, transcribed by hand.
 *
 * Field names match the server's exactly. There is no renaming layer on
 * purpose: a rename is a place for a field to quietly disappear, and every
 * field here is one the console is supposed to show rather than interpret.
 */

export type AgentResponse = {
  id: string;
  name: string;
  alias: string;
  status: string;
  current_version_id: string | null;
  created_at: string;
};

/** The subset of the agent spec the phase-2C form edits. */
/**
 * Discriminated the same way the server discriminates it, so a policy the
 * console does not understand is a type error here rather than a silently
 * dropped field on the way back out.
 */
export type ModelPolicyDocument =
  | { provider: "deterministic"; scenario: string }
  | {
      provider: "openai_compatible";
      endpoint_id: string;
      temperature?: number | null;
      max_output_tokens?: number | null;
    };

export type ModelEndpointSummary = {
  id: string;
  name: string;
  model: string;
  context_window: number;
  max_output_tokens: number;
  usage_quality: string;
  context_accounting: string;
  tokenizer: string | null;
  status: string;
};

export type ModelEndpointDetail = ModelEndpointSummary & {
  kind: string;
  base_url: string;
  credential_available: boolean;
};

export type EndpointCheckResponse = {
  reachable: boolean;
  elapsed_ms: number;
  refusal: string | null;
  detail: string | null;
};

export type AgentSpecDocument = {
  schema_version: number;
  personality: string;
  model_policy: ModelPolicyDocument;
  tools: string[];
  limits: {
    max_execution_seconds: number;
    max_elapsed_seconds: number;
    max_model_calls: number;
    max_tool_calls: number;
    max_derived_retries: number;
  };
  delivery?: {
    enabled: boolean;
    sync_timeout_seconds: number;
  };
  /**
   * What this Agent may reach on the network, fixed at publish like `tools`.
   *
   * Absent means nothing: an Agent that never asked for the network does not
   * get it because its workspace has some. Every entry has to be inside what
   * the workspace approved, and publishing says so if it is not.
   */
  network?: { allow: string[] };
  /**
   * Bound skills, by version id and never by name.
   *
   * Publishing a new version of a skill therefore changes nothing about an
   * Agent already published against an older one. Switching is this list being
   * edited and the Agent being published again — deliberately two acts.
   */
  skills?: { skill_version_id: string }[];
  /**
   * Bound HTTP operations, by document version id and never by tool name.
   *
   * `write_policy` is required at publish for any bound operation that writes:
   * §16.3 wants the choice made rather than defaulted, because all three
   * answers are defensible and none is safe to assume.
   */
  http_tools?: {
    http_tool_version_id: string;
    operations: string[];
    write_policy: WritePolicy | null;
  }[];
  /**
   * Bound MCP tools, by server snapshot id and an explicit name subset. There
   * is deliberately no way to say "all of them" — see `agentMcpHint`.
   *
   * `write_policy` is required for *every* MCP binding rather than only for
   * ones that write, because an MCP server does not say which of its tools
   * change something and the platform cannot tell.
   */
  mcp_tools?: {
    mcp_server_version_id: string;
    tools: string[];
    write_policy: WritePolicy | null;
  }[];
};

export type AgentDraftResponse = {
  agent_id: string;
  revision: number;
  spec: AgentSpecDocument;
  updated_at: string;
};

export type AgentVersionResponse = {
  id: string;
  agent_id: string;
  version_number: number;
  schema_version: number;
  content_hash: string;
  created_at: string;
};

export type AgentVersionDetailResponse = AgentVersionResponse & {
  spec: AgentSpecDocument;
};

export type SessionResponse = {
  id: string;
  agent_id: string;
  session_mode: string;
  caller_type: string;
  caller_id: string;
  head_run_id: string | null;
  next_run_sequence: number;
  next_message_sequence: number;
  created_at: string;
};

export type QueueResponse = {
  position: number;
  status: string;
  blocked_by_run_id?: string | null;
  head_status?: string;
  head_reason?: {
    pause_reason: string | null;
    wait_kind: string | null;
    wait_deadline_at: string | null;
  };
  available_actions?: string[];
};

export type BudgetDocument = {
  max_execution_seconds: number;
  consumed_execution_ms: number;
  max_elapsed_seconds: number;
  elapsed_deadline_at: string;
  max_model_calls: number;
  consumed_model_calls: number;
  max_tool_calls: number;
  consumed_tool_calls: number;
  max_tokens: number | null;
  consumed_tokens: number;
  max_derived_retries: number;
  derived_retry_count: number;
  /**
   * The most this Run may spend, as a decimal string, or null for no limit.
   *
   * A string rather than a number all the way to the screen: JSON numbers are
   * floats, and money that went through one is money two people can disagree
   * about.
   */
  max_cost: string | null;
  cost_currency: string | null;
  /**
   * What it has spent. **Null is unknown, never zero.** A Run whose endpoint
   * has no configured price never gets a number here, and the console says
   * "unknown" in words rather than showing a nought.
   */
  consumed_cost: string | null;
  /** How that number was arrived at: `provider`, `estimated` or `unknown`. */
  cost_quality: string;
};

/** Which round the Run is on, and what the platform decided about it. */
export type GoalDocument = {
  round: number | null;
  outcome: string | null;
  unmet: string[];
};

export type ChildRunRef = {
  id: string;
  status: string;
};

export type RunResponse = {
  id: string;
  session_id: string;
  agent_version_id: string;
  status: string;
  state_version: number;
  session_sequence: number;
  blocked_by_run_id: string | null;
  pause_reason: string | null;
  wait_kind: string | null;
  wait_deadline_at: string | null;
  retry_of_run_id: string | null;
  budget_root_run_id: string;
  /** The Run that delegated this one, or null for one somebody asked for. */
  parent_run_id: string | null;
  /** 0 for a Run a caller created, 1 for a delegated one. Never more. */
  depth: number;
  /** The Runs this one delegated, oldest first. Empty for most Runs. */
  children: ChildRunRef[];
  last_event_sequence: number;
  queue: QueueResponse;
  budget: BudgetDocument;
  available_actions: string[];
  checkpoint_replay_safe: boolean;
  checkpoint_effect_status: string;
  goal: GoalDocument;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

/** One decoded frame from the event stream. */
export type RunEventFrame = {
  sequence: number;
  event_type: string;
  occurred_at: string;
  payload: Record<string, unknown>;
};

export type CanonicalMessagePart = {
  type: string;
  text?: string;
  call_id?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  output?: string;
  exit_code?: number;
  failed?: boolean;
};

export type CanonicalMessage = {
  role: string;
  parts: CanonicalMessagePart[];
};

export type ArtifactResponse = {
  id: string;
  run_id: string;
  session_id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  truncated: boolean;
  expires_at: string;
};

export type WorkspaceMemberResponse = {
  user_id: string;
  display_name: string;
  subject: string;
  role: string;
};

export type ServiceAccountResponse = {
  id: string;
  workspace_id: string;
  name: string;
  role: string;
  status: string;
  created_by_user_id: string;
  created_at: string;
};

export type ApiKeyResponse = {
  id: string;
  service_account_id: string;
  prefix: string;
  scopes: string[];
  agent_ids: string[];
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

export type IssuedApiKeyResponse = ApiKeyResponse & {
  token: string;
};

export type SecretResponse = {
  id: string;
  name: string;
  scope: "workspace" | "platform";
  workspace_id: string | null;
  status: string;
  mask: string;
  created_at: string;
  updated_at: string;
};

export type RewrapResponse = {
  processed: number;
  remaining: number;
  current_key_id: string;
};

/** Scopes a developer ServiceAccount may request. Viewer keys are a subset. */
export const API_KEY_SCOPES = ["runs.read", "runs.write", "runs.control", "agents.read"] as const;

export const VIEWER_API_KEY_SCOPES = ["runs.read", "agents.read"] as const;

/** Every tool this platform implements. An Agent may bind a subset. */
export const IMPLEMENTED_TOOLS = [
  "file.list",
  "file.read",
  "file.write",
  "platform.wait",
  "shell.exec",
  "skill.load",
  "skill.propose",
  // §14.1 and §14.3. Listed here as well as in the backend's registry for the
  // reason the scenarios are: an author who cannot tick the box cannot bind
  // the tool, and the feature is unreachable from the console it shipped with.
  "memory.remember",
  "session.search",
  // §13. Same reason again: an Agent that cannot be given `agent.delegate`
  // from the builder cannot delegate at all, and `artifact.read` is how
  // whatever it is handed actually gets opened.
  "agent.delegate",
  "artifact.read",
] as const;

/**
 * Every scenario the deterministic substitute implements.
 *
 * The list the builder offers, so a scenario the platform can run but the
 * console does not name is a Run nobody can start from here — which is how
 * `waiting_external` went unreachable through the UI for a while.
 */
export const MODEL_SCENARIOS = [
  "complete",
  "continue_once",
  "fail_replay_safe",
  "shell_once",
  "shell_from_input",
  "propose_once",
  "skill_once",
  "wait_once",
  // The two external-tool drills. Listed here as well as declared in the
  // backend's `DeterministicModelPolicy`, because an author who cannot select
  // one cannot exercise the path it exists for.
  "http_once",
  "mcp_once",
  "remember_once",
  "search_once",
  "delegate_once",
] as const;

export type SkillResponse = {
  id: string;
  scope: "platform" | "workspace";
  workspace_id: string | null;
  name: string;
  current_version_id: string | null;
  created_at: string;
  updated_at: string;
};

export type FindingResponse = {
  code: string;
  severity: "blocking" | "advisory";
  path: string;
  detail: string;
};

export type SkillVersionResponse = {
  id: string;
  skill_id: string;
  version_number: number;
  content_hash: string;
  name: string;
  description: string;
  findings: FindingResponse[];
  source: "upload" | "git" | "proposal";
  source_url: string | null;
  source_ref: string | null;
  status: "active" | "withdrawn";
  /** Whether a draft may name this version. The console asks rather than
   * recomputing "active and unblocked", so the button and the publish check
   * cannot disagree. */
  bindable: boolean;
  created_at: string;
};

export type SkillFilePayload = {
  path: string;
  content: string;
};

export type ProposalResponse = {
  id: string;
  skill_id: string | null;
  base_version_id: string | null;
  name: string;
  description: string;
  findings: FindingResponse[];
  origin: "agent" | "human";
  origin_run_id: string | null;
  status: "pending" | "approved" | "rejected";
  approvable: boolean;
  created_by: string;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
};

export type DiffLineResponse = {
  kind: "context" | "added" | "removed" | "skipped";
  text: string;
};

export type FileDiffResponse = {
  path: string;
  change: "added" | "removed" | "changed";
  lines: DiffLineResponse[];
  added_lines: number;
  removed_lines: number;
  truncated: boolean;
};

export type ProposalDetailResponse = ProposalResponse & {
  files: SkillFilePayload[];
  diff: FileDiffResponse[];
};

export type OutboundScopeEntry = {
  id: string;
  level: "platform" | "workspace";
  workspace_id: string | null;
  entry: string;
  note: string | null;
  /**
   * True when a model endpoint owns this entry. It appears when the endpoint is
   * registered and disappears when the endpoint is disabled, so the console
   * shows no remove control for one — a button that always loses is worse than
   * no button.
   */
  managed: boolean;
  created_at: string;
};

/**
 * What happens when a bound tool would change something at the far end.
 *
 * All three are defensible and none is a safe default, which is why publishing
 * refuses a binding that chose nothing.
 */
export type WritePolicy = "disabled" | "preauthorized" | "governance";

/** One HTTP tool a workspace registered. */
export type HttpToolResponse = {
  id: string;
  workspace_id: string;
  name: string;
  base_url: string;
  credential_ref: string | null;
  current_version_id: string | null;
  created_at: string;
  updated_at: string;
};

export type HttpOperationResponse = {
  operation_id: string;
  method: string;
  path: string;
  summary: string | null;
  /** False for anything that changes data, which is what needs an approval. */
  read_only: boolean;
};

export type HttpToolVersionResponse = {
  id: string;
  http_tool_id: string;
  version_number: number;
  content_hash: string;
  title: string;
  document_version: string;
  operations: HttpOperationResponse[];
  status: string;
  bindable: boolean;
  created_at: string;
};

/** One MCP server a workspace registered. */
export type McpServerResponse = {
  id: string;
  workspace_id: string;
  name: string;
  url: string;
  credential_ref: string | null;
  current_version_id: string | null;
  /**
   * When this platform last got an answer out of it. "Registered" and
   * "reachable" are different facts and the list shows both.
   */
  last_validated_at: string | null;
  created_at: string;
  updated_at: string;
};

export type McpToolResponse = {
  name: string;
  description: string | null;
  input_schema: Record<string, unknown>;
};

export type McpServerVersionResponse = {
  id: string;
  mcp_server_id: string;
  version_number: number;
  content_hash: string;
  tools: McpToolResponse[];
  status: string;
  bindable: boolean;
  created_at: string;
};

/** One request for a person's decision. */
export type ApprovalResponse = {
  id: string;
  run_id: string;
  /**
   * `user_confirmation` may only be answered by the EndUser who started the
   * Run; `governance_approval` only by a workspace or platform administrator.
   * The console shows the two apart because they are two different powers.
   */
  approval_type: "user_confirmation" | "governance_approval";
  status: string;
  tool: string;
  /** The normalized call, exactly as it was hashed. */
  document: Record<string, unknown>;
  required_permission: string | null;
  requested_by: string;
  expires_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
};

/** What an endpoint charges, as one recorded version. */
export type PricingVersionResponse = {
  id: string;
  endpoint_id: string;
  version_number: number;
  currency: string;
  input_per_million: string;
  output_per_million: string;
  cached_input_per_million: string | null;
  /**
   * True when an administrator declared this endpoint free. Its own field so
   * nothing has to infer it from two zeroes — "priced at nothing" and "not
   * priced" are different states and only this one is a price.
   */
  free: boolean;
  effective_at: string;
  created_by: string;
  created_at: string;
};
