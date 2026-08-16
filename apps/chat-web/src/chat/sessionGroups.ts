export type DatedSession = {
  id: string;
  title: string;
  createdAt: string;
};

/** Which band of the rail a conversation falls into. */
export type GroupKey = "today" | "yesterday" | "week" | "earlier";

export type SessionGroup<T> = {
  key: GroupKey;
  sessions: T[];
};

const DAY = 24 * 60 * 60 * 1000;

function startOfDay(at: Date): number {
  return new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime();
}

/**
 * Which band a timestamp belongs to, judged in local calendar days.
 *
 * Calendar days rather than elapsed hours: a conversation from 23:50 is
 * "yesterday" at 00:10, not "an hour ago", and a rail that disagrees with the
 * clock on the wall is worse than one with no dates at all.
 */
export function groupOf(createdAt: string, now: Date = new Date()): GroupKey {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) {
    return "earlier";
  }
  const today = startOfDay(now);
  const day = startOfDay(at);
  if (day >= today) {
    return "today";
  }
  if (day >= today - DAY) {
    return "yesterday";
  }
  if (day >= today - 6 * DAY) {
    return "week";
  }
  return "earlier";
}

const ORDER: GroupKey[] = ["today", "yesterday", "week", "earlier"];

/**
 * The rail's bands, newest first inside each, empty bands omitted.
 *
 * Input order is preserved within a band, so whatever the caller decided
 * about recency still holds; this only decides where the headings go.
 */
export function groupSessions<T extends { createdAt: string }>(
  sessions: T[],
  now: Date = new Date(),
): SessionGroup<T>[] {
  const bands = new Map<GroupKey, T[]>();
  for (const session of sessions) {
    const key = groupOf(session.createdAt, now);
    const band = bands.get(key);
    if (band === undefined) {
      bands.set(key, [session]);
    } else {
      band.push(session);
    }
  }
  return ORDER.filter((key) => (bands.get(key)?.length ?? 0) > 0).map((key) => ({
    key,
    sessions: bands.get(key) ?? [],
  }));
}

/**
 * Conversations whose title contains every word typed, in any order.
 *
 * Word-wise rather than substring: somebody looking for a chat about a weekly
 * report types "weekly report", and the title may read "the report, weekly".
 * Case is folded; an empty query matches everything rather than nothing.
 */
export function filterSessions<T extends { title: string }>(sessions: T[], query: string): T[] {
  const words = query.toLowerCase().split(/\s+/).filter((word) => word !== "");
  if (words.length === 0) {
    return sessions;
  }
  return sessions.filter((session) => {
    const title = session.title.toLowerCase();
    return words.every((word) => title.includes(word));
  });
}
