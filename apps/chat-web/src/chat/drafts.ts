const PREFIX = "tiny-hermes-chat-draft:";
const PENDING = "tiny-hermes-chat-pending:";
const FILES = "tiny-hermes-chat-draft-files:";

export function loadDraftFiles(key: string | undefined): string[] {
  if (key === undefined) return [];
  try {
    const value: unknown = JSON.parse(window.sessionStorage.getItem(FILES + key) ?? "[]");
    return Array.isArray(value) ? value.filter((name): name is string => typeof name === "string").slice(0, 8) : [];
  } catch { return []; }
}

export function saveDraftFiles(key: string, names: string[]): boolean {
  try {
    if (names.length === 0) window.sessionStorage.removeItem(FILES + key);
    else window.sessionStorage.setItem(FILES + key, JSON.stringify(names));
    return true;
  } catch { return false; }
}

export function clearAllDrafts(): void {
  try {
    for (const key of Object.keys(window.sessionStorage)) {
      if ([PREFIX, PENDING, FILES].some((prefix) => key.startsWith(prefix))) window.sessionStorage.removeItem(key);
    }
  } catch { /* Clearing this tab must remain possible when storage is unavailable. */ }
}

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
    window.sessionStorage.removeItem(FILES + key);
  } catch { /* Storage failures must not prevent chatting. */ }
}

export function moveDraft(from: string, to: string): void {
  if (from === to) return;
  const textSaved = saveDraftText(to, loadDraftText(from));
  const filesSaved = saveDraftFiles(to, loadDraftFiles(from));
  if (textSaved && filesSaved) clearDraft(from);
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
