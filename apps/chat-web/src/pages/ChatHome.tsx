import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { EndUserAgentResponse } from "../api/types";
import { chooseDefaultAgent, loadDefaultAgent } from "../chat/defaultAgent";
import { chatPath } from "../chat/paths";
import { useT } from "../i18n/locale";

/**
 * `/` for somebody who already holds a session: straight into the Agent
 * this device prefers, or the first one the credential allows. A visitor
 * with no session sees the same waiting text as before — the credential
 * arrives in the URL fragment from the host page, and nothing here can
 * conjure one.
 *
 * Asked directly rather than through the query cache: the cache treats a
 * 401 as a session that was *lost*, and on this page a 401 more often means
 * a session that never was. "等待接入" is the honest answer to that; "连接已
 * 断开" is not.
 */
export function ChatHome() {
  const t = useT();
  const [agents, setAgents] = useState<EndUserAgentResponse[] | null | undefined>(undefined);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api<EndUserAgentResponse[]>("/api/v1/end-user/agents")
      .then((listed) => {
        if (!cancelled) setAgents(listed);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setAgents(null);
          setError(!(caught instanceof ApiError && [401, 403].includes(caught.status)));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  if (error) return <main className="auth"><p role="alert">{t("requestFailed")}</p><button onClick={() => { setError(false); setAgents(undefined); setAttempt(attempt + 1); }}>{t("retry")}</button></main>;

  if (agents === undefined) {
    return <p className="centered">{t("loading")}</p>;
  }
  const chosen = chooseDefaultAgent(agents ?? [], loadDefaultAgent());
  if (chosen !== undefined) {
    return <Navigate to={chatPath(chosen.alias)} replace />;
  }
  return (
    <main className="auth">
      <h1>{t(agents === null ? "connectWaitingTitle" : "noAvailableAgents")}</h1>
      <p className="auth-intro">{t(agents === null ? "connectWaitingHint" : "noAvailableAgentsHint")}</p>
    </main>
  );
}
