import { useState } from "react";
import { Link } from "react-router-dom";

import { problemMessage } from "../api/messages";
import { useAuth } from "../auth/AuthProvider";
import type { ListedAgent } from "../chat/published";
import { usePublishedAgents } from "../chat/usePublishedAgents";
import {
  loadDefaultAgent,
  sameDefaultAgent,
  saveDefaultAgent,
  type DefaultAgentRef,
} from "../i18n/defaultAgent";
import { useT } from "../i18n/locale";

function groupByWorkspace(rows: ListedAgent[]): Array<{
  workspaceId: string;
  workspaceName: string;
  rows: ListedAgent[];
}> {
  const groups: Array<{ workspaceId: string; workspaceName: string; rows: ListedAgent[] }> = [];
  for (const row of rows) {
    const current = groups.find((group) => group.workspaceId === row.workspace.id);
    if (current === undefined) {
      groups.push({
        workspaceId: row.workspace.id,
        workspaceName: row.workspace.name,
        rows: [row],
      });
    } else {
      current.rows.push(row);
    }
  }
  return groups;
}

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
            <dd>{auth.user?.displayName ?? "—"}</dd>
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
          <div className="settings-agent-groups">
            {groupByWorkspace(listed.rows).map((group) => (
              <section key={group.workspaceId} className="settings-agent-group">
                <h3>{group.workspaceName}</h3>
                <ul className="settings-agent-list">
                  {group.rows.map((row) => {
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
                          <strong>{row.agent.name}</strong>
                          <span>{row.agent.alias}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
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
