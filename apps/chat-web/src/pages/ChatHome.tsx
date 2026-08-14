import { Navigate } from "react-router-dom";

import { problemMessage } from "../api/messages";
import { SessionRail } from "../chat/SessionRail";
import { usePublishedAgents } from "../chat/usePublishedAgents";
import { chooseDefaultAgent, loadDefaultAgent } from "../i18n/defaultAgent";
import { useT } from "../i18n/locale";

export function ChatHome() {
  const t = useT();
  const listed = usePublishedAgents();

  if (listed.pending) {
    return <p className="centered">{t("loading")}</p>;
  }
  if (listed.error !== undefined) {
    return (
      <p className="centered">
        {problemMessage(listed.error)}
        <button type="button" onClick={listed.refetch}>
          {t("retry")}
        </button>
      </p>
    );
  }
  const chosen = chooseDefaultAgent(listed.rows, loadDefaultAgent());
  if (chosen !== undefined) {
    return <Navigate to={`/${chosen.workspace.id}/${chosen.agent.id}`} replace />;
  }

  return (
    <div className="chat-app">
      <SessionRail
        sessions={[]}
        activeSessionId={null}
        onSession={() => undefined}
        onNewChat={() => undefined}
        creating={false}
        newChatDisabled
      />
      <section className="chat-main">
        <div className="thread-empty">
          <p>{t("emptyAgents")}</p>
        </div>
      </section>
    </div>
  );
}
