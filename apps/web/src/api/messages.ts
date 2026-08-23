import { ApiError } from "./client";
import type { MessageKey } from "../i18n/zh-CN";

/**
 * Codes the console has its own wording for.
 *
 * Everything absent here falls through to the server's `detail`, which is
 * already a sentence written for a person. Inventing a message for a code this
 * table does not know would be the console guessing what the platform meant.
 */
const NAMED: Partial<Record<string, MessageKey>> = {
  agent_alias_taken: "agentAliasTaken",
  invalid_agent_alias: "invalidAgentAlias",
  invalid_agent_name: "invalidAgentName",
  invalid_agent_spec: "invalidAgentSpec",
  draft_revision_conflict: "draftRevisionConflict",
  agent_not_published: "agentNotPublished",
  idempotency_key_reused: "idempotencyKeyReused",
  state_version_conflict: "stateVersionConflict",
  forbidden: "forbidden",
  workspace_scope_mismatch: "forbidden",
  user_not_found: "userNotFound",
  member_already_present: "memberAlreadyPresent",
  last_workspace_admin: "lastWorkspaceAdmin",
  // The two the transport itself raises. They live here rather than in
  // `client.ts` so that nothing below this file needs a locale at all: the
  // transport produces codes, and one place turns codes into sentences.
  network_failed: "networkFailed",
  request_failed: "requestFailed",
};

/** Which form field a refusal belongs to, when it belongs to one. */
const FIELDS: Partial<Record<string, string>> = {
  agent_alias_taken: "alias",
  invalid_agent_alias: "alias",
  invalid_agent_name: "name",
};

/**
 * The caller passes its own `t` because this module has no locale of its own.
 *
 * It used to import `t` from `../i18n/zh-CN` directly, so every mapped code
 * rendered in Chinese however the reader had set the language — the console's
 * switcher changed the chrome around a message that never moved. The signature
 * now makes a return to that a compile error rather than a wrong string.
 */
export function problemMessage(error: unknown, t: (key: MessageKey) => string): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : t("requestFailed");
  }
  const key = NAMED[error.code];
  // An empty `message` means the platform sent no `detail` — there is nothing
  // to fall through to, so this says the one true thing it can.
  if (key === undefined) {
    return error.message === "" ? t("requestFailed") : error.message;
  }
  return t(key);
}

export function problemField(error: unknown): string | null {
  return error instanceof ApiError ? (FIELDS[error.code] ?? null) : null;
}

/** Whether the browser's session is the thing that failed, rather than the request. */
export function isSessionLost(error: unknown): boolean {
  return (
    error instanceof ApiError && (error.status === 401 || error.code === "csrf_failed")
  );
}
