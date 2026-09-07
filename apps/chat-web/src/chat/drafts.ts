const PREFIX = "tiny-hermes-chat-draft:";

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
