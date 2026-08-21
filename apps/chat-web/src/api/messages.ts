import { ApiError } from "./client";
import type { MessageKey } from "../i18n/zh-CN";

const NAMED: Partial<Record<string, MessageKey>> = {
  agent_not_published: "agentNotPublished",
  idempotency_key_reused: "idempotencyKeyReused",
  state_version_conflict: "stateVersionConflict",
  forbidden: "forbidden",
  workspace_scope_mismatch: "forbidden",
  approval_reason_required: "approvalReasonRequired",
  approval_expired: "approvalExpired",
  approval_already_decided: "approvalAlreadyDecided",
};

/**
 * The caller passes its own `t` because this module has no locale of its own.
 * It used to import `t` from `../i18n/zh-CN` directly, which meant every
 * mapped error code rendered in Chinese no matter what the user had chosen —
 * the one place in the app where the locale switch silently did nothing, and
 * the place a confused user is most likely to be looking.
 */
export function problemMessage(error: unknown, t: (key: MessageKey) => string): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : t("requestFailed");
  }
  const key = NAMED[error.code];
  return key === undefined ? error.message : t(key);
}

export function isSessionLost(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.code === "csrf_failed");
}
