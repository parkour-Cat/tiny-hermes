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
  onSession,
  onNewChat,
  creating,
  newChatDisabled = false,
}: {
  sessions: RailSession[];
  activeSessionId: string | null;
  onSession: (id: string) => void;
  onNewChat: () => void;
  creating: boolean;
  newChatDisabled?: boolean;
}) {
  const t = useT();

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
        {sessions.length === 0 ? <p className="rail-empty">{t("emptySessions")}</p> : null}
        {sessions.map((session) => (
          <button
            type="button"
            key={session.id}
            className={session.id === activeSessionId ? "is-active" : ""}
            aria-current={session.id === activeSessionId ? "true" : undefined}
            onClick={() => onSession(session.id)}
          >
            {session.title}
          </button>
        ))}
      </nav>
      <div className="rail-foot">
        <UserMenu />
      </div>
    </aside>
  );
}
