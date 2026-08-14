import { Navigate } from "react-router-dom";

import { problemMessage } from "../api/messages";
import { SessionRail } from "../chat/SessionRail";
import { usePublishedAgents } from "../chat/usePublishedAgents";
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
  const first = listed.rows[0];
  if (first !== undefined) {
    return <Navigate to={`/${first.workspace.id}/${first.agent.id}`} replace />;
  }

  return (
    <div className="chat-app">
      <SessionRail
        agents={[]}
        agentKey=""
        sessions={[]}
        activeSessionId={null}
        onAgent={() => undefined}
        onSession={() => undefined}
        onNewChat={() => undefined}
        creating={false}
      />
      <section className="chat-main">
        <div className="thread-empty">
          <p>{t("emptyAgents")}</p>
        </div>
      </section>
    </div>
  );
}
