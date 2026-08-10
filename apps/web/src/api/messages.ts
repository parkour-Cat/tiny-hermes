import { ApiError } from "./client";
import type { MessageKey } from "../i18n/zh-CN";
import { t } from "../i18n/zh-CN";

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
  forbidden: "forbidden",
  workspace_scope_mismatch: "forbidden",
};

/** Which form field a refusal belongs to, when it belongs to one. */
const FIELDS: Partial<Record<string, string>> = {
  agent_alias_taken: "alias",
  invalid_agent_alias: "alias",
  invalid_agent_name: "name",
};

export function problemMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : t("requestFailed");
  }
  const key = NAMED[error.code];
  return key === undefined ? error.message : t(key);
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
