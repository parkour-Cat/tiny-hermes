const KEY = "tiny-hermes-chat-default-agent";

export type DefaultAgentRef = {
  workspaceId: string;
  agentId: string;
};

export function loadDefaultAgent(): DefaultAgentRef | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw === null) {
      return null;
    }
    const parsed = JSON.parse(raw) as DefaultAgentRef;
    if (
      typeof parsed.workspaceId !== "string" ||
      typeof parsed.agentId !== "string" ||
      parsed.workspaceId === "" ||
      parsed.agentId === ""
    ) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function saveDefaultAgent(ref: DefaultAgentRef): void {
  window.localStorage.setItem(KEY, JSON.stringify(ref));
}

export function sameDefaultAgent(a: DefaultAgentRef | null, b: DefaultAgentRef): boolean {
  return a?.workspaceId === b.workspaceId && a.agentId === b.agentId;
}

export function chooseDefaultAgent<T extends { workspace: { id: string }; agent: { id: string } }>(
  agents: T[],
  preferred: DefaultAgentRef | null,
): T | undefined {
  if (preferred !== null) {
    const match = agents.find(
      (row) => row.workspace.id === preferred.workspaceId && row.agent.id === preferred.agentId,
    );
    if (match !== undefined) {
      return match;
    }
  }
  return agents[0];
}
