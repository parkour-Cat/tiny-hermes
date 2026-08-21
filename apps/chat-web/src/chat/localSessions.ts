/**
 * Which Sessions this browser has opened with a given Agent alias.
 *
 * There is no `GET /api/v1/end-user/sessions` (design §5 never built a list
 * endpoint for an end user — `session_router` is console-only) so the rail
 * a returning visitor sees is this device's own memory of what it opened,
 * not the platform's. That is not a workaround standing in for a real list:
 * §4.5.1 already made this device the only place a magic-link-style
 * "resume anywhere" story could live, on `defaultAgent`'s own precedent
 * ("只记在这台设备上"). The conversation itself is never local — every id
 * here still has to resolve through `GET .../sessions/{id}/messages`,
 * which is the platform, ownership-checked by the cookie, saying yes.
 */

const KEY_PREFIX = "tiny-hermes-chat-end-user-sessions:";

function key(alias: string): string {
  return `${KEY_PREFIX}${alias}`;
}

export function loadKnownSessionIds(alias: string): string[] {
  try {
    const raw = window.localStorage.getItem(key(alias));
    if (raw === null) {
      return [];
    }
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : [];
  } catch {
    return [];
  }
}

export function rememberSessionId(alias: string, sessionId: string): void {
  const known = loadKnownSessionIds(alias);
  if (known.includes(sessionId)) {
    return;
  }
  try {
    window.localStorage.setItem(key(alias), JSON.stringify([sessionId, ...known]));
  } catch {
    // Best-effort: a blocked store loses the rail's memory, not the chat.
  }
}

export function forgetSessionId(alias: string, sessionId: string): void {
  try {
    window.localStorage.setItem(
      key(alias),
      JSON.stringify(loadKnownSessionIds(alias).filter((id) => id !== sessionId)),
    );
  } catch {
    // Best-effort, same as rememberSessionId.
  }
}

/**
 * Task-9 review finding H: every other action here is scoped to one Agent
 * alias, which is right for `forgetSessionId` (a device forgetting a
 * conversation with one Agent has no reason to touch its memory of any
 * other) but leaves nothing a caller can use as a plain "sign out of this
 * device" — that would mean already knowing every alias this device ever
 * talked to, which nothing here tracks. This walks every key this module
 * itself ever wrote (`KEY_PREFIX`, never a bare guess at `localStorage`'s
 * own key list) and clears them all, alias by alias, the same best-effort
 * shape every other write here already has: a blocked store loses the
 * rail's memory, not the chat.
 */
export function forgetAllSessionIds(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const stored = window.localStorage.key(i);
      if (stored !== null && stored.startsWith(KEY_PREFIX)) {
        keys.push(stored);
      }
    }
    for (const stored of keys) {
      window.localStorage.removeItem(stored);
    }
  } catch {
    // Best-effort, same as every other write in this module.
  }
}
