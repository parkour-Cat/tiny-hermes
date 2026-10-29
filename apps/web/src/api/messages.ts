import { ApiError } from "./client";
import type { MessageKey } from "../i18n/zh-CN";

/**
 * Codes the console has its own wording for.
 *
 * Everything absent here falls through to the server's `detail`, which is
 * already a sentence written for a person — in English, which is why
 * `messages.test.ts` reads every code out of the backend and fails on one
 * neither table knows. Inventing a message for a code this table does not
 * know would be the console guessing what the platform meant.
 */
const NAMED: Partial<Record<string, MessageKey>> = {
  model_discovery_unauthorized: "quickUnauthorized",
  model_discovery_unsupported: "quickUnsupported",
  model_discovery_failed: "quickDiscoveryFailed",
  model_discovery_invalid: "quickInvalidModels",
  model_discovery_invalid_request: "quickInvalidRequest",
  endpoint_name_taken: "quickNameTaken",
  context_budget_unsatisfied: "contextBudgetDoesNotFit",
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
  agent_example_not_found: "errAgentExampleNotFound",
  agent_network_unavailable: "errAgentNetworkUnavailable",
  agent_not_found: "errAgentNotFound",
  agent_request_rejected: "errAgentRequestRejected",
  api_key_not_found: "errApiKeyNotFound",
  approval_already_decided: "errApprovalAlreadyDecided",
  approval_expired: "errApprovalExpired",
  approval_not_found: "errApprovalNotFound",
  approval_reason_required: "errApprovalReasonRequired",
  artifact_not_found: "errArtifactNotFound",
  audit_export_too_large: "errAuditExportTooLarge",
  bootstrap_closed: "errBootstrapClosed",
  budget_not_widened: "errBudgetNotWidened",
  channel_already_bound: "errChannelAlreadyBound",
  channel_binding_not_found: "errChannelBindingNotFound",
  channel_issuer_already_registered: "errChannelIssuerAlreadyRegistered",
  channel_issuer_not_found: "errChannelIssuerNotFound",
  channel_transport_unusable: "errChannelTransportUnusable",
  credential_missing: "errCredentialMissing",
  csrf_failed: "errCsrfFailed",
  end_user_not_found: "errEndUserNotFound",
  endpoint_window_inconsistent: "errEndpointWindowInconsistent",
  event_cursor_too_old: "errEventCursorTooOld",
  event_sequence_conflict: "errEventSequenceConflict",
  http_tool_name_taken: "errHttpToolNameTaken",
  http_tool_not_found: "errHttpToolNotFound",
  http_tool_version_not_found: "errHttpToolVersionNotFound",
  idempotency_key_required: "errIdempotencyKeyRequired",
  invalid_agent_update: "errInvalidAgentUpdate",
  invalid_api_key_scopes: "errInvalidApiKeyScopes",
  invalid_bootstrap_token: "errInvalidBootstrapToken",
  invalid_credentials: "errInvalidCredentials",
  invalid_member_role: "errInvalidMemberRole",
  invalid_secret_name: "errInvalidSecretName",
  invalid_secret_plaintext: "errInvalidSecretPlaintext",
  invalid_service_account_name: "errInvalidServiceAccountName",
  invalid_service_account_role: "errInvalidServiceAccountRole",
  invalid_subject_query: "errInvalidSubjectQuery",
  invalid_time_range: "errInvalidTimeRange",
  invalid_workspace_id: "errInvalidWorkspaceId",
  invalid_workspace_name: "errInvalidWorkspaceName",
  kek_missing: "errKekMissing",
  login_throttled: "errLoginThrottled",
  mcp_server_name_taken: "errMcpServerNameTaken",
  mcp_server_not_found: "errMcpServerNotFound",
  mcp_server_version_not_found: "errMcpServerVersionNotFound",
  member_not_found: "errMemberNotFound",
  memory_already_decided: "errMemoryAlreadyDecided",
  memory_not_found: "errMemoryNotFound",
  model_endpoint_not_found: "errModelEndpointNotFound",
  model_endpoint_unavailable: "errModelEndpointUnavailable",
  model_output_limit_too_high: "errModelOutputLimitTooHigh",
  oidc_login_failed: "errOidcLoginFailed",
  oidc_provider_not_available: "errOidcProviderNotAvailable",
  oidc_provider_not_found: "errOidcProviderNotFound",
  oidc_provider_unreachable: "errOidcProviderUnreachable",
  outbound_entry_managed: "errOutboundEntryManaged",
  outbound_entry_not_found: "errOutboundEntryNotFound",
  previous_kek_missing: "errPreviousKekMissing",
  run_request_rejected: "errRunRequestRejected",
  secret_name_taken: "errSecretNameTaken",
  secret_not_found: "errSecretNotFound",
  service_account_name_taken: "errServiceAccountNameTaken",
  service_account_not_found: "errServiceAccountNotFound",
  site_not_found: "errSiteNotFound",
  skill_bound_twice: "errSkillBoundTwice",
  skill_name_taken: "errSkillNameTaken",
  skill_not_found: "errSkillNotFound",
  skill_proposal_not_found: "errSkillProposalNotFound",
  skill_scan_refused: "errSkillScanRefused",
  skill_summary_budget_exceeded: "errSkillSummaryBudgetExceeded",
  skill_version_not_bindable: "errSkillVersionNotBindable",
  skill_version_not_found: "errSkillVersionNotFound",
  subject_not_found: "errSubjectNotFound",
  unauthenticated: "errUnauthenticated",
  workspace_file_not_found: "errWorkspaceFileNotFound",
  workspace_files_changed: "errWorkspaceFilesChanged",
  workspace_files_unavailable: "errWorkspaceFilesUnavailable",
  workspace_not_found: "errWorkspaceNotFound",
  workspace_required: "errWorkspaceRequired",
  // The two the transport itself raises. They live here rather than in
  // `client.ts` so that nothing below this file needs a locale at all: the
  // transport produces codes, and one place turns codes into sentences.
  network_failed: "networkFailed",
  request_failed: "requestFailed",
};

/**
 * Codes whose `detail` the server composes from one of many reasons — a
 * validator's, a remote server's, an importer's. The code says what kind of
 * thing went wrong and is worded here; which reason it was lives only in the
 * sentence, so that sentence follows as it came rather than being guessed at.
 */
const WITH_REASON: Partial<Record<string, MessageKey>> = {
  outbound_entry_outside_platform: "errOutboundEntryOutsidePlatform",
  host_outside_workspace_scope: "errHostOutsideWorkspaceScope",
  agent_network_outside_workspace: "errAgentNetworkOutsideWorkspace",
  context_window_too_small: "errContextWindowTooSmall",
  invalid_approval_filter: "errInvalidApprovalFilter",
  invalid_audit_filter: "errInvalidAuditFilter",
  invalid_base_url: "errInvalidBaseUrl",
  invalid_channel_issuer: "errInvalidChannelIssuer",
  invalid_http_tool_name: "errInvalidHttpToolName",
  invalid_mcp_capabilities: "errInvalidMcpCapabilities",
  invalid_mcp_server_name: "errInvalidMcpServerName",
  invalid_mcp_url: "errInvalidMcpUrl",
  invalid_memory_body: "errInvalidMemoryBody",
  invalid_oidc_provider: "errInvalidOidcProvider",
  invalid_openapi_document: "errInvalidOpenapiDocument",
  invalid_outbound_entry: "errInvalidOutboundEntry",
  invalid_price: "errInvalidPrice",
  invalid_search: "errInvalidSearch",
  invalid_skill_package: "errInvalidSkillPackage",
  limit_out_of_range: "errLimitOutOfRange",
  mcp_server_unreachable: "errMcpServerUnreachable",
  run_not_steerable: "errRunNotSteerable",
  skill_binding_unavailable: "errSkillBindingUnavailable",
  skill_import_failed: "errSkillImportFailed",
  skill_name_mismatch: "errSkillNameMismatch",
  skill_proposal_not_approvable: "errSkillProposalNotApprovable",
};

/**
 * Refusals that carry their numbers in `context` (`asked`, `allowed`).
 *
 * Worded here, in the reader's language, with the numbers put in. Without
 * them — an older API, or a proxy that dropped `context` — the server's own
 * sentence is shown instead: true in English beats Chinese with holes in it.
 */
const CEILINGS: Partial<Record<string, MessageKey>> = {
  sandbox_memory_ceiling_exceeded: "ceilingSandboxMemory",
  execution_ceiling_exceeded: "ceilingExecution",
  round_ceiling_exceeded: "ceilingModelCalls",
  tool_call_ceiling_exceeded: "ceilingToolCalls",
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
  const ceiling = CEILINGS[error.code];
  const { asked, allowed } = error.context;
  if (ceiling !== undefined && typeof asked === "number" && typeof allowed === "number") {
    return t(ceiling).replace("{asked}", String(asked)).replace("{allowed}", String(allowed));
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
  // An empty `message` means the platform sent no `detail` — there is nothing
  // to fall through to, so this says the one true thing it can.
  if (key === undefined) {
    return error.message === "" ? t("requestFailed") : error.message;
  }
  return t(key);
}

/** Whether this module words `code` itself, rather than passing the server's sentence on. */
export function hasWording(code: string): boolean {
  return NAMED[code] !== undefined || WITH_REASON[code] !== undefined || CEILINGS[code] !== undefined;
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
