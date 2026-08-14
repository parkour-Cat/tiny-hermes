import type { ListedAgent } from "./published";
import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { useChatTheme } from "../theme/ChatTheme";
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
  const auth = useAuth();
  const { t, locale, setLocale } = useLocale();
  const theme = useChatTheme();
  const initial = (auth.user?.display_name ?? "?").slice(0, 1).toUpperCase();

  return (
    <aside className="rail">
      <div className="rail-brand">
        <HermesMark size={32} />
        <span className="th-word">{t("appName")}</span>
      </div>
      {agents.length === 0 ? null : (
        <select
          className="rail-agent"
          aria-label={t("pickAgent")}
          value={agentKey}
          onChange={(event) => onAgent(event.target.value)}
        >
          {agents.map((row) => (
            <option
              key={`${row.workspace.id}:${row.agent.id}`}
              value={`${row.workspace.id}:${row.agent.id}`}
            >
              {row.agent.name}
              {agents.some(
                (other) =>
                  other.agent.name === row.agent.name && other.workspace.id !== row.workspace.id,
              )
                ? ` · ${row.workspace.name}`
                : ""}
            </option>
          ))}
        </select>
      )}
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
        <div className="rail-user">
          <span className="rail-avatar" aria-hidden>
            {initial}
          </span>
          <span>{auth.user?.display_name}</span>
        </div>
        <div className="rail-tools">
          <button type="button" onClick={() => theme.toggle()}>
            {theme.dark ? t("themeLight") : t("themeDark")}
          </button>
          <button
            type="button"
            aria-label={t("language")}
            onClick={() => setLocale(locale === "zh-CN" ? "en-US" : "zh-CN")}
          >
            {locale === "zh-CN" ? t("localeEn") : t("localeZh")}
          </button>
          <button type="button" onClick={() => void auth.logout()}>
            {t("logout")}
          </button>
        </div>
      </div>
    </aside>
  );
}
