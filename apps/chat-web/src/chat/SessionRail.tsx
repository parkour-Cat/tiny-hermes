import { useState } from "react";

import { SessionItem } from "./SessionItem";
import { filterSessions, groupSessions, type GroupKey } from "./sessionGroups";
import type { MessageKey } from "../i18n/zh-CN";
import {
  arrangeSessions,
  hideSession,
  setArchived,
  setPinned,
  type SessionPrefs,
} from "./sessionPrefs";
import { UserMenu } from "./UserMenu";
import { useT } from "../i18n/locale";
import { HermesMark } from "../ui/HermesMark";

export type RailSession = {
  id: string;
  title: string;
  createdAt: string;
};

const GROUP_LABELS: Record<GroupKey, MessageKey> = {
  today: "groupToday",
  yesterday: "groupYesterday",
  week: "groupWeek",
  earlier: "groupEarlier",
};

export function SessionRail({
  sessions,
  activeSessionId,
  prefs,
  onPrefs,
  onSession,
  onNewChat,
  onHidden,
  creating,
  newChatDisabled = false,
}: {
  sessions: RailSession[];
  activeSessionId: string | null;
  prefs: SessionPrefs;
  onPrefs: (prefs: SessionPrefs) => void;
  onSession: (id: string) => void;
  onNewChat: () => void;
  onHidden?: (id: string) => void;
  creating: boolean;
  newChatDisabled?: boolean;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const arranged = arrangeSessions(filterSessions(sessions, query), prefs);
  // Pinned rows keep their own band at the top: a person pins a conversation
  // to stop it moving, and filing it under a date would move it every day.
  const pinned = arranged.open.filter((session) => prefs.pinned.includes(session.id));
  const dated = groupSessions(arranged.open.filter((session) => !prefs.pinned.includes(session.id)));
  const nothingMatches =
    query.trim() !== "" && arranged.open.length === 0 && arranged.archived.length === 0;

  function item(session: RailSession, archived: boolean) {
    return (
      <SessionItem
        key={session.id}
        session={session}
        active={session.id === activeSessionId}
        pinned={prefs.pinned.includes(session.id)}
        archived={archived}
        onOpen={() => onSession(session.id)}
        onPin={(pinned) => onPrefs(setPinned(prefs, session.id, pinned))}
        onArchive={(next) => onPrefs(setArchived(prefs, session.id, next))}
        onForget={() => {
          onPrefs(hideSession(prefs, session.id));
          onHidden?.(session.id);
        }}
      />
    );
  }

  return (
    <aside className="rail">
      <div className="rail-brand">
        <HermesMark size={22} />
        <span className="th-word">{t("appName")}</span>
      </div>
      <button
        type="button"
        className="rail-new"
        disabled={creating || newChatDisabled}
        onClick={onNewChat}
      >
        <span aria-hidden>+</span>
        {t("newChat")}
      </button>
      {/* A rail you can take in at a glance does not need a filter; five rows
          is about where scanning stops working. It stays once typed in, so
          clearing the box cannot make the box disappear. */}
      {sessions.length >= 5 || query !== "" ? (
        <input
          className="rail-search"
          type="search"
          value={query}
          placeholder={t("searchSessions")}
          aria-label={t("searchSessions")}
          onChange={(event) => setQuery(event.target.value)}
        />
      ) : null}
      <nav className="rail-sessions" aria-label={t("sessions")}>
        {nothingMatches ? <p className="rail-empty">{t("noMatchingSessions")}</p> : null}
        {arranged.open.length === 0 && arranged.archived.length === 0 && !nothingMatches ? (
          <p className="rail-empty">{t("emptySessions")}</p>
        ) : null}
        {pinned.length > 0 ? (
          <>
            <p className="rail-kicker">{t("groupPinned")}</p>
            {pinned.map((session) => item(session, false))}
          </>
        ) : null}
        {dated.map((group) => (
          <div key={group.key}>
            <p className="rail-kicker">{t(GROUP_LABELS[group.key])}</p>
            {group.sessions.map((session) => item(session, false))}
          </div>
        ))}
        {arranged.archived.length > 0 ? (
          <>
            <p className="rail-kicker rail-kicker-sub">{t("archivedSessions")}</p>
            {arranged.archived.map((session) => item(session, true))}
          </>
        ) : null}
      </nav>
      <div className="rail-foot">
        <UserMenu />
      </div>
    </aside>
  );
}
