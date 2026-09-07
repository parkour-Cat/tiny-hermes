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

export function readAgentDraft(key: string): LocalAgentDraft | null {
  const raw = sessionStorage.getItem(key);
  return raw === null ? null : JSON.parse(raw) as LocalAgentDraft;
}

export function writeAgentDraft(key: string, draft: LocalAgentDraft | null): void {
  if (draft === null) sessionStorage.removeItem(key);
  else sessionStorage.setItem(key, JSON.stringify(draft));
}
