import type { FormValues } from "./AgentDetailPage";

export type LocalAgentDraft = {
  revision: number;
  name: string;
  alias: string;
  values: FormValues;
};

export function agentDraftKey(user: string, workspace: string, agent: string): string {
  return `tiny-hermes:agent-edits:v1:${JSON.stringify([user, workspace, agent])}`;
}

function validValues(value: unknown): value is FormValues {
  if (!value || typeof value !== "object") return false;
  const fields = value as Record<string, unknown>;
  const strings = ["personality", "name", "alias", "scenario"];
  const lists = ["tools", "skills", "network", "http_tools", "mcp_tools"];
  const numbers = ["max_execution_seconds", "max_elapsed_seconds", "max_model_calls", "max_tool_calls", "max_derived_retries", "sync_timeout_seconds"];
  return strings.every((key) => typeof fields[key] === "string") &&
    lists.every((key) => Array.isArray(fields[key]) && fields[key].every((item) => typeof item === "string")) &&
    numbers.every((key) => fields[key] === null || typeof fields[key] === "number") &&
    ["delivery_enabled", "end_user_access_enabled"].every((key) => typeof fields[key] === "boolean") &&
    ["deterministic", "openai_compatible"].includes(String(fields.provider)) &&
    (fields.endpoint_id === undefined || typeof fields.endpoint_id === "string") &&
    (fields.skill_review_enabled === undefined || typeof fields.skill_review_enabled === "boolean") &&
    (fields.skill_review_min_tool_calls === undefined ||
      fields.skill_review_min_tool_calls === null ||
      typeof fields.skill_review_min_tool_calls === "number") &&
    // Absent in a draft kept before fallbacks existed; `specOf` reads it as none.
    (fields.fallback_endpoint_ids === undefined ||
      (Array.isArray(fields.fallback_endpoint_ids) &&
        fields.fallback_endpoint_ids.every((item) => typeof item === "string"))) &&
    ["http_write_policy", "mcp_write_policy"].every((key) => fields[key] === undefined ||
      ["disabled", "preauthorized", "governance"].includes(String(fields[key])));
}

export function readAgentDraft(key: string): { draft: LocalAgentDraft | null; failed: boolean } {
  try {
    const raw = sessionStorage.getItem(key);
    if (raw === null) return { draft: null, failed: false };
    const data = JSON.parse(raw) as Partial<LocalAgentDraft> | null;
    if (!data || !Number.isInteger(data.revision) || typeof data.name !== "string" ||
      typeof data.alias !== "string" || !validValues(data.values)) return { draft: null, failed: true };
    return { draft: data as LocalAgentDraft, failed: false };
  } catch {
    return { draft: null, failed: true };
  }
}

export function writeAgentDraft(key: string, draft: LocalAgentDraft | null): boolean {
  try {
    if (draft === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(draft));
    return sessionStorage.getItem(key) === (draft === null ? null : JSON.stringify(draft));
  } catch {
    return false;
  }
}
