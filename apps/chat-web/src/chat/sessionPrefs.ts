const KEY = "tiny-hermes-chat-session-prefs";

export type SessionPrefs = {
  pinned: string[];
  archived: string[];
  hidden: string[];
};

export function emptySessionPrefs(): SessionPrefs {
  return { pinned: [], archived: [], hidden: [] };
}

function ids(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item !== "");
}

export function loadSessionPrefs(): SessionPrefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw === null) {
      return emptySessionPrefs();
    }
    const parsed = JSON.parse(raw) as Partial<SessionPrefs>;
    return {
      pinned: ids(parsed.pinned),
      archived: ids(parsed.archived),
      hidden: ids(parsed.hidden),
    };
  } catch {
    return emptySessionPrefs();
  }
}

export function saveSessionPrefs(prefs: SessionPrefs): void {
  window.localStorage.setItem(KEY, JSON.stringify(prefs));
}

function without(list: string[], id: string): string[] {
  return list.filter((item) => item !== id);
}

function withFirst(list: string[], id: string): string[] {
  return [id, ...without(list, id)];
}

export function setPinned(prefs: SessionPrefs, id: string, pinned: boolean): SessionPrefs {
  return {
    ...prefs,
    pinned: pinned ? withFirst(prefs.pinned, id) : without(prefs.pinned, id),
  };
}

export function setArchived(prefs: SessionPrefs, id: string, archived: boolean): SessionPrefs {
  return {
    pinned: archived ? without(prefs.pinned, id) : prefs.pinned,
    archived: archived ? withFirst(prefs.archived, id) : without(prefs.archived, id),
    hidden: prefs.hidden,
  };
}

export function hideSession(prefs: SessionPrefs, id: string): SessionPrefs {
  return {
    pinned: without(prefs.pinned, id),
    archived: without(prefs.archived, id),
    hidden: withFirst(prefs.hidden, id),
  };
}

export function arrangeSessions<T extends { id: string }>(
  sessions: T[],
  prefs: SessionPrefs,
): { open: T[]; archived: T[] } {
  const visible = sessions.filter((session) => !prefs.hidden.includes(session.id));
  const archived = visible.filter((session) => prefs.archived.includes(session.id));
  const rest = visible.filter((session) => !prefs.archived.includes(session.id));
  const pinned = prefs.pinned.flatMap((id) => rest.filter((session) => session.id === id));
  const unpinned = rest.filter((session) => !prefs.pinned.includes(session.id));
  return { open: [...pinned, ...unpinned], archived };
}
