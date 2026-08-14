import { useCallback, useRef, useState } from "react";

import { agentLabel, type ListedAgent } from "./published";
import { useDismiss } from "./useDismiss";
import { useT } from "../i18n/locale";

export function AgentPicker({
  agents,
  agentKey,
  onAgent,
  fallback,
}: {
  agents: ListedAgent[];
  agentKey: string;
  onAgent: (key: string) => void;
  fallback?: string;
}) {
  const t = useT();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);

  const current = agents.find((row) => `${row.workspace.id}:${row.agent.id}` === agentKey);
  const label = current === undefined ? (fallback ?? t("pickAgent")) : agentLabel(current, agents);

  if (agents.length === 0) {
    return <h1>{fallback ?? t("pickAgent")}</h1>;
  }

  return (
    <div className="title-picker-wrap" ref={root}>
      <button
        type="button"
        className="title-picker"
        aria-label={t("pickAgent")}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <h1>{label}</h1>
        <span className="menu-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open ? (
        <ul className="menu-panel title-picker-menu" role="listbox" aria-label={t("pickAgent")}>
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
