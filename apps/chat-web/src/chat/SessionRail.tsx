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

  return (
    <aside className="rail">
      <div className="rail-brand">
        <HermesMark size={36} />
        <span className="th-word">{t("appName")}</span>
      </div>
      {agents.length === 0 ? null : (
        <label className="rail-agent">
          {t("pickAgent")}
          <select
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
                {agents.some((other) => other.agent.name === row.agent.name && other.workspace.id !== row.workspace.id)
                  ? ` · ${row.workspace.name}`
                  : ""}
              </option>
            ))}
          </select>
        </label>
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
        <span className="rail-user">{auth.user?.display_name}</span>
        <button type="button" onClick={() => theme.toggle()}>
          {theme.dark ? t("themeLight") : t("themeDark")}
        </button>
        <select
          aria-label={t("language")}
          value={locale}
          onChange={(event) => setLocale(event.target.value === "en-US" ? "en-US" : "zh-CN")}
        >
          <option value="zh-CN">{t("localeZh")}</option>
          <option value="en-US">{t("localeEn")}</option>
        </select>
        <button type="button" onClick={() => void auth.logout()}>
          {t("logout")}
        </button>
      </div>
    </aside>
  );
}
