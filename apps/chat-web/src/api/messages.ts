import { ApiError } from "./client";
import type { MessageKey } from "../i18n/zh-CN";
import { t } from "../i18n/zh-CN";

const NAMED: Partial<Record<string, MessageKey>> = {
  agent_not_published: "agentNotPublished",
  idempotency_key_reused: "idempotencyKeyReused",
  state_version_conflict: "stateVersionConflict",
  forbidden: "forbidden",
  workspace_scope_mismatch: "forbidden",
};

export function problemMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : t("requestFailed");
  }
  const key = NAMED[error.code];
  return key === undefined ? error.message : t(key);
}

export function isSessionLost(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.code === "csrf_failed");
}
