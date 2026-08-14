import { asId } from "./ids";
import type { ListedAgent } from "./published";

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function agentRef(row: ListedAgent, agents: ListedAgent[]): string {
  const clash = agents.some(
    (other) => other.agent.alias === row.agent.alias && other.workspace.id !== row.workspace.id,
  );
  return clash ? `${row.agent.alias}--${shortId(row.workspace.id)}` : row.agent.alias;
}

export function chatPath(
  row: ListedAgent,
  agents: ListedAgent[],
  sessionId?: string | null,
): string {
  const base = `/${agentRef(row, agents)}`;
  return sessionId === undefined || sessionId === null ? base : `${base}/${shortId(sessionId)}`;
}

export function resolveAgentRef(ref: string, agents: ListedAgent[]): ListedAgent | undefined {
  const split = ref.indexOf("--");
  if (split > 0) {
    const alias = ref.slice(0, split);
    const mark = ref.slice(split + 2);
    return agents.find(
      (row) => row.agent.alias === alias && row.workspace.id.startsWith(mark),
    );
  }
  const matches = agents.filter((row) => row.agent.alias === ref);
  return matches.length === 1 ? matches[0] : undefined;
}

export function matchSessionId(ids: string[], ref: string | null): string | null {
  if (ref === null) {
    return null;
  }
  const exact = ids.find((id) => id === ref);
  if (exact !== undefined) {
    return exact;
  }
  const prefixed = ids.filter((id) => id.startsWith(ref));
  return prefixed[0] ?? null;
}

export type ChatRoute =
  | { kind: "ok"; workspaceId: string; agentId: string; sessionRef: string | null }
  | { kind: "pending" }
  | { kind: "invalid" };

export function resolveChatRoute(
  parts: { left?: string; middle?: string; right?: string },
  agents: ListedAgent[],
): ChatRoute {
  const left = parts.left;
  const middle = parts.middle;
  const right = parts.right;
  if (left === undefined) {
    return { kind: "invalid" };
  }
  if (asId(left) !== null && middle !== undefined && asId(middle) !== null) {
    return { kind: "ok", workspaceId: left, agentId: middle, sessionRef: right ?? null };
  }
  if (agents.length === 0) {
    return { kind: "pending" };
  }
  const row = resolveAgentRef(left, agents);
  if (row === undefined) {
    return { kind: "invalid" };
  }
  return {
    kind: "ok",
    workspaceId: row.workspace.id,
    agentId: row.agent.id,
    sessionRef: middle ?? null,
  };
}
