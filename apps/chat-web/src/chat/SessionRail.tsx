import { SessionItem } from "./SessionItem";
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
  const arranged = arrangeSessions(sessions, prefs);

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
      <p className="rail-kicker">{t("sessions")}</p>
      <nav className="rail-sessions" aria-label={t("sessions")}>
        {arranged.open.length === 0 && arranged.archived.length === 0 ? (
          <p className="rail-empty">{t("emptySessions")}</p>
        ) : null}
        {arranged.open.map((session) => item(session, false))}
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
