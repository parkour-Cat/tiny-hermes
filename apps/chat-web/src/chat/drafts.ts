const PREFIX = "tiny-hermes-chat-draft:";
const PENDING = "tiny-hermes-chat-pending:";

export type PendingSend = { text: string; key: string };

export function loadPendingSend(key: string): PendingSend | null {
  try {
    const value: unknown = JSON.parse(window.sessionStorage.getItem(PENDING + key) ?? "null");
    if (typeof value !== "object" || value === null) return null;
    const record = value as Partial<PendingSend>;
    return typeof record.text === "string" && typeof record.key === "string" ? record as PendingSend : null;
  } catch { return null; }
}

export function savePendingSend(key: string, pending: PendingSend): void {
  try { window.sessionStorage.setItem(PENDING + key, JSON.stringify(pending)); }
  catch { /* The in-memory request identity still protects retries in this page. */ }
}

export function clearDraft(key: string): void {
  try {
    window.sessionStorage.removeItem(PREFIX + key);
    window.sessionStorage.removeItem(PENDING + key);
  } catch { /* Storage failures must not prevent chatting. */ }
}

export function moveDraft(from: string, to: string): void {
  if (from === to) return;
  if (saveDraftText(to, loadDraftText(from))) clearDraft(from);
}

export function loadDraftText(key: string | undefined): string {
  if (key === undefined) return "";
  try {
    return window.sessionStorage.getItem(PREFIX + key) ?? "";
  } catch {
    return "";
  }
}

export function saveDraftText(key: string, text: string): boolean {
  try {
    if (text === "") window.sessionStorage.removeItem(PREFIX + key);
    else window.sessionStorage.setItem(PREFIX + key, text);
    return true;
  } catch {
    return false;
  }
}
