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

/**
 * Task-9 review finding F: the end-user `POST .../sessions` route used to
 * return the console's own `SessionResponse` shape verbatim —
 * `caller_type`/`caller_id`/`head_run_id`/`next_run_sequence`/
 * `next_message_sequence` are the platform's own Session bookkeeping, no
 * more this app's business than the fields already trimmed off
 * `RunResponse` below. The backend narrowed its response model to match
 * (`EndUserSessionResponse` in `runs/presentation/end_user_routes.py`); this
 * type is that model, not the console's.
 */
export type EndUserSessionResponse = {
  id: string;
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

/**
 * Plan §10: the end-user shape a chat surface reads for its own Run —
 * `EndUserRunResponse` in `runs/presentation/end_user_routes.py`, narrower
 * than the console's `RunResponse` above for the same reason task-7 review
 * finding 4 records there. `state_version` is the one addition §10 made to
 * that model: the number `cancelEndUserRun` (`runs/useEndUserRun.ts`) has
 * to echo back as `expected_state_version`.
 */
export type EndUserRunResponse = {
  id: string;
  session_id: string;
  status: string;
  state_version: number;
  finished_at: string | null;
  queue: QueueResponse;
};

/**
 * §10's other missing door: `ApprovalResponse` in
 * `runs/presentation/approval_routes.py`, reused as-is by
 * `GET /api/v1/end-user/approvals` and
 * `POST /api/v1/end-user/approvals/{id}/decision` — the backend found
 * nothing console-operational in it worth hiding from the end user an
 * approval already belongs to (see that route's own docstring).
 */
export type ApprovalResponse = {
  id: string;
  run_id: string;
  approval_type: string;
  status: string;
  tool: string;
  document: Record<string, unknown>;
  required_permission: string | null;
  requested_by: string;
  expires_at: string;
  decided_by?: string | null;
  decided_at?: string | null;
  decision_reason?: string | null;
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

export type MemoryResponse = {
  id: string;
  workspace_id: string;
  agent_id: string;
  kind: string;
  status: string;
  body: string;
  origin: string;
  created_at: string;
  updated_at: string;
};

/** Design §4.6's self-service export — an end user's own subject data, the
 * same shape `subject_routes.py` returns a workspace member for their own
 * `CallerIdentity`. */
export type SubjectExportResponse = {
  subject_type: string;
  subject_id: string;
  workspace_id: string;
  memories: MemoryResponse[];
  sessions: string[];
};

export type ErasureResponse = {
  memories: number;
  sessions: number;
  messages: number;
  artifacts: number;
};
