import { AgentPicker } from "./AgentPicker";
import type { ListedAgent } from "./published";
import { UserMenu } from "./UserMenu";
import { useT } from "../i18n/locale";
import { HermesMark } from "../ui/HermesMark";

export type RailSession = {
  id: string;
  title: string;
};

export function SessionRail({
  agents,
  agentKey,
  sessions,
  activeSessionId,
  onAgent,
  onSession,
  onNewChat,
  creating,
}: {
  agents: ListedAgent[];
  agentKey: string;
  sessions: RailSession[];
  activeSessionId: string | null;
  onAgent: (key: string) => void;
  onSession: (id: string) => void;
  onNewChat: () => void;
  creating: boolean;
}) {
  const t = useT();

  return (
    <aside className="rail">
      <div className="rail-brand">
        <HermesMark size={32} />
        <span className="th-word">{t("appName")}</span>
      </div>
      <AgentPicker agents={agents} agentKey={agentKey} onAgent={onAgent} />
      <button type="button" className="rail-new" disabled={creating} onClick={onNewChat}>
        {t("newChat")}
      </button>
      <nav className="rail-sessions" aria-label={t("sessions")}>
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
