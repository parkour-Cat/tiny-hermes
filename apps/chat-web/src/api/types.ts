/**
 * The committed response shapes chat-web reads.
 *
 * Field names match the server's exactly. This app only keeps the fields a
 * conversation needs; management documents stay in the console.
 */

export type AgentResponse = {
  id: string;
  name: string;
  alias: string;
  status: string;
  current_version_id: string | null;
  created_at: string;
};

export type WorkspaceResponse = {
  id: string;
  name: string;
  status: string;
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
