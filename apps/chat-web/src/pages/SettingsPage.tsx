import { useState } from "react";
import { Link } from "react-router-dom";

import { problemMessage } from "../api/messages";
import { useAuth } from "../auth/AuthProvider";
import { agentLabel } from "../chat/published";
import { usePublishedAgents } from "../chat/usePublishedAgents";
import {
  loadDefaultAgent,
  sameDefaultAgent,
  saveDefaultAgent,
  type DefaultAgentRef,
} from "../i18n/defaultAgent";
import { useT } from "../i18n/locale";

export function SettingsPage() {
  const t = useT();
  const auth = useAuth();
  const listed = usePublishedAgents();
  const [preferred, setPreferred] = useState(loadDefaultAgent);

  return (
    <main className="settings">
      <Link to="/" className="settings-back">
        {t("backToChat")}
      </Link>
      <h1>{t("settings")}</h1>
      <p className="settings-intro">{t("settingsIntro")}</p>
      <section>
        <h2>{t("account")}</h2>
        <dl className="settings-dl">
          <div>
            <dt>{t("displayName")}</dt>
            <dd>{auth.user?.display_name ?? "—"}</dd>
          </div>
          <div>
            <dt>{t("email")}</dt>
            <dd>{auth.user?.subject ?? "—"}</dd>
          </div>
        </dl>
      </section>
      <section>
        <h2>{t("defaultAgent")}</h2>
        <p className="settings-hint">{t("defaultAgentHint")}</p>
        {listed.pending ? <p className="settings-hint">{t("loading")}</p> : null}
        {listed.error !== undefined ? (
          <p className="settings-hint">
            {problemMessage(listed.error)}
            <button type="button" onClick={listed.refetch}>
              {t("retry")}
            </button>
          </p>
        ) : null}
        {!listed.pending && listed.error === undefined && listed.rows.length === 0 ? (
          <p className="settings-hint">{t("emptyAgents")}</p>
        ) : null}
        {listed.rows.length > 0 ? (
          <ul className="settings-agent-list">
            {listed.rows.map((row) => {
              const ref: DefaultAgentRef = {
                workspaceId: row.workspace.id,
                agentId: row.agent.id,
              };
              const selected = sameDefaultAgent(preferred, ref);
              return (
                <li key={`${row.workspace.id}:${row.agent.id}`}>
                  <button
                    type="button"
                    className={selected ? "is-on" : undefined}
                    aria-pressed={selected}
                    onClick={() => {
                      saveDefaultAgent(ref);
                      setPreferred(ref);
                    }}
                  >
                    <strong>{agentLabel(row, listed.rows)}</strong>
                    <span>{row.workspace.name}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
      </section>
      <section className="settings-about">
        <h2>{t("about")}</h2>
        <p className="settings-hint">{t("aboutBody")}</p>
        <p className="settings-meta">{t("appVersion")}</p>
      </section>
    </main>
  );
}
