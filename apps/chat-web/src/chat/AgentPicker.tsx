import { useCallback, useRef, useState } from "react";

import { useDismiss } from "./useDismiss";
import type { EndUserAgentResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { ChevronDown } from "../ui/ChevronDown";

/**
 * The conversation's title: the Agent's name, and — only when the credential
 * names more than one — a menu to move to another. The list is exactly what
 * `GET /api/v1/end-user/agents` answered; nothing here can name an Agent the
 * enterprise did not.
 */
export function AgentPicker({
  agents,
  alias,
  onAgent,
}: {
  agents: EndUserAgentResponse[];
  alias: string;
  onAgent: (alias: string) => void;
}) {
  const t = useT();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);

  const label = agents.find((agent) => agent.alias === alias)?.name ?? alias;

  if (agents.length < 2) {
    return <h1>{label}</h1>;
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
        <ChevronDown />
      </button>
      {open ? (
        <ul className="menu-panel title-picker-menu" role="listbox" aria-label={t("pickAgent")}>
          {agents.map((agent) => {
            const selected = agent.alias === alias;
            return (
              <li key={agent.alias} role="presentation">
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={selected ? "is-selected" : ""}
                  onClick={() => {
                    onAgent(agent.alias);
                    setOpen(false);
                  }}
                >
                  {agent.name}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
