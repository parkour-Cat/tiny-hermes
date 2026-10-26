import { useQuery } from "@tanstack/react-query";
import { Modal } from "antd";
import { useId, useMemo, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";

import { NAV_GROUPS, visibleSections } from "./navigation";
import { api } from "../api/client";
import type { AgentResponse } from "../api/types";
import { useT } from "../i18n/locale";
import type { Role } from "../workspace/useMyRole";

const RUN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type Entry = { key: string; label: string; kind: string; to: string; terms?: string };

/**
 * Search the console by name, and jump.
 *
 * Only what this browser can already list without asking for more: the
 * sections the sidebar would show this role (by label, by the sentence that
 * introduces them, and by the words people use for them), the Agents, and a
 * Run by its full id. Secrets, outbound hosts and task content would need either a new
 * search endpoint or pulling sensitive lists down to filter here, so they are
 * not offered — and the placeholder does not suggest they are.
 */
export function CommandPalette({
  workspaceId,
  role,
  isPlatformAdmin,
  open,
  onClose,
}: {
  workspaceId: string;
  role: Role | null;
  isPlatformAdmin: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const navigate = useNavigate();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const agents = useQuery({
    queryKey: ["agents", workspaceId],
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", { workspace: workspaceId }),
    enabled: open,
  });

  const sections = useMemo<Entry[]>(() => {
    if (role === null) return [];
    return NAV_GROUPS.flatMap((group) => {
      const visible = visibleSections(group, role, isPlatformAdmin);
      if (visible.length === 0) return [];
      if (group.sections.length === 1) {
        const only = group.sections[0]!;
        return [{
          key: `page:${group.key}`,
          label: t(group.labelKey),
          kind: t("palettePages"),
          to: only.path,
          terms: [t(group.introKey), only.searchKey === undefined ? "" : t(only.searchKey)].join(" "),
        }];
      }
      return visible.map((section) => ({
        key: `page:${group.key}:${section.key}`,
        label: `${t(group.labelKey)} › ${t(section.labelKey)}`,
        kind: t("palettePages"),
        to: `${group.key}#${section.key}`,
        terms: [t(section.introKey ?? group.introKey), section.searchKey === undefined ? "" : t(section.searchKey)].join(" "),
      }));
    });
  }, [role, isPlatformAdmin, t]);

  const needle = query.trim().toLowerCase();
  const entries: Entry[] = [];
  if (RUN_ID.test(query.trim())) {
    entries.push({ key: "run", label: `${t("paletteOpenRun")} ${query.trim()}`, kind: t("runs"), to: `runs/${query.trim()}` });
  }
  if (needle !== "") {
    entries.push(...sections.filter((entry) => `${entry.label} ${entry.terms ?? ""}`.toLowerCase().includes(needle)));
    for (const agent of agents.data ?? []) {
      if (`${agent.name} ${agent.alias}`.toLowerCase().includes(needle)) {
        entries.push({ key: `agent:${agent.id}`, label: agent.name, kind: t("agents"), to: `agents/${agent.id}` });
      }
    }
  } else {
    entries.push(...sections);
  }
  const current = Math.min(active, Math.max(entries.length - 1, 0));

  function close(): void {
    setQuery("");
    setActive(0);
    onClose();
  }

  function go(entry: Entry | undefined): void {
    if (entry === undefined) return;
    close();
    navigate(`/workspaces/${workspaceId}/${entry.to}`);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>): void {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current + 1) % Math.max(entries.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current - 1 + entries.length) % Math.max(entries.length, 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(entries[current]);
    }
  }

  return (
    <Modal
      open={open}
      onCancel={close}
      footer={null}
      closable={false}
      title={t("commandPalette")}
      className="th-palette"
      width={640}
      destroyOnHidden
    >
      <input
        className="th-palette-input"
        role="combobox"
        aria-expanded="true"
        aria-controls={listId}
        aria-activedescendant={entries.length === 0 ? undefined : `${listId}-${current}`}
        aria-label={t("commandPalette")}
        placeholder={t("palettePlaceholder")}
        value={query}
        autoFocus
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      <ul id={listId} role="listbox" className="th-palette-list" aria-label={t("commandPalette")}>
        {entries.map((entry, index) => (
          <li
            key={entry.key}
            id={`${listId}-${index}`}
            role="option"
            aria-selected={index === current}
            className="th-palette-option"
            onMouseEnter={() => setActive(index)}
            onClick={() => go(entry)}
          >
            <span className="th-palette-label">{entry.label}</span>
            <span className="th-palette-kind">{entry.kind}</span>
          </li>
        ))}
      </ul>
      {entries.length === 0 ? <p className="th-palette-empty">{t("paletteEmpty")}</p> : null}
    </Modal>
  );
}
