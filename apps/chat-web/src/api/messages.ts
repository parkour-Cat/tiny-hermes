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
  end_user_origin_not_allowed: "errEndUserOriginNotAllowed",
  unauthenticated: "errUnauthenticated",
  end_user_credential_invalid: "errEndUserCredentialInvalid",
  end_user_credential_lifetime_exceeds_ceiling: "errEndUserCredentialLifetime",
  end_user_access_gate_closed: "errEndUserAccessGateClosed",
  end_user_agent_not_found: "errEndUserAgentNotFound",
  memory_not_found: "errMemoryNotFound",
  approval_not_found: "errApprovalNotFound",
  budget_not_widened: "errBudgetNotWidened",
  event_sequence_conflict: "errEventSequenceConflict",
  idempotency_key_required: "errIdempotencyKeyRequired",
  run_request_rejected: "errRunRequestRejected",
  workspace_file_not_found: "errWorkspaceFileNotFound",
  workspace_files_changed: "errWorkspaceFilesChanged",
  workspace_files_unavailable: "errWorkspaceFilesUnavailable",
  // The two the transport itself raises: it has no locale, so it leaves
  // the sentence to this table.
  network_failed: "networkFailed",
  request_failed: "requestFailed",
};

/**
 * Codes whose `detail` the server composes from one of several reasons. The
 * code is worded here; which reason it was lives only in the sentence, so that
 * sentence follows as it came rather than being guessed at.
 */
const WITH_REASON: Partial<Record<string, MessageKey>> = {
  run_not_steerable: "errRunNotSteerable",
  invalid_memory_body: "errInvalidMemoryBody",
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
  const headline = WITH_REASON[error.code];
  if (headline !== undefined) {
    return error.message === ""
      ? t(headline)
      : t("problemWithReason").replace("{headline}", t(headline)).replace("{reason}", error.message);
  }
  // FastAPI's own refusal of a body it could not read carries no code. Saying
  // "try again" there invites the same request, which can only fail the same way.
  if (error.code === "request_failed" && error.status === 422) {
    return t("requestInvalid");
  }
  const key = NAMED[error.code];
  if (key === undefined) {
    return error.message === "" ? t("requestFailed") : error.message;
  }
  return t(key);
}

/** Whether this module words `code` itself, rather than passing the server's sentence on. */
export function hasWording(code: string): boolean {
  return NAMED[code] !== undefined || WITH_REASON[code] !== undefined;
}

export function isSessionLost(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.code === "csrf_failed");
}
