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
export type AgentSpecDocument = {
  schema_version: number;
  personality: string;
  model_policy: { provider: string; scenario: string };
  tools: unknown[];
  limits: {
    max_execution_seconds: number;
    max_elapsed_seconds: number;
    max_model_calls: number;
    max_tool_calls: number;
    max_derived_retries: number;
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

/** The three scenarios the deterministic substitute actually implements. */
export const MODEL_SCENARIOS = ["complete", "continue_once", "fail_replay_safe"] as const;
