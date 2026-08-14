import { useCallback, useRef, useState } from "react";

import { agentLabel, type ListedAgent } from "./published";
import { useDismiss } from "./useDismiss";
import { useT } from "../i18n/locale";

export function AgentPicker({
  agents,
  agentKey,
  onAgent,
}: {
  agents: ListedAgent[];
  agentKey: string;
  onAgent: (key: string) => void;
}) {
  const t = useT();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);

  const current = agents.find((row) => `${row.workspace.id}:${row.agent.id}` === agentKey);
  const label = current === undefined ? t("pickAgent") : agentLabel(current, agents);

  if (agents.length === 0) {
    return null;
  }

  return (
    <div className="menu-anchor" ref={root}>
      <button
        type="button"
        className="menu-trigger"
        aria-label={t("pickAgent")}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{label}</span>
        <span className="menu-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open ? (
        <ul className="menu-panel" role="listbox" aria-label={t("pickAgent")}>
          {agents.map((row) => {
            const key = `${row.workspace.id}:${row.agent.id}`;
            const selected = key === agentKey;
            return (
              <li key={key} role="presentation">
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={selected ? "is-selected" : ""}
                  onClick={() => {
                    onAgent(key);
                    setOpen(false);
                  }}
                >
                  {agentLabel(row, agents)}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
