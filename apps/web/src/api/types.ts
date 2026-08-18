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
};

/** Which round the Run is on, and what the platform decided about it. */
export type GoalDocument = {
  round: number | null;
  outcome: string | null;
  unmet: string[];
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
] as const;
