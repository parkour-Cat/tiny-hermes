import type { AgentResponse, SessionResponse, WorkspaceResponse } from "../api/types";

export type ListedAgent = {
  workspace: WorkspaceResponse;
  agent: AgentResponse;
};

export function publishedIn(
  workspaces: WorkspaceResponse[],
  listings: Array<AgentResponse[] | undefined>,
): ListedAgent[] {
  return workspaces.flatMap((workspace, index) =>
    (listings[index] ?? [])
      .filter((agent) => agent.current_version_id !== null)
      .map((agent) => ({ workspace, agent })),
  );
}

export function agentLabel(row: ListedAgent, agents: ListedAgent[]): string {
  const clash = agents.some(
    (other) => other.agent.name === row.agent.name && other.workspace.id !== row.workspace.id,
  );
  return clash ? `${row.agent.name} · ${row.workspace.name}` : row.agent.name;
}

export function matchingSessions(
  listed: SessionResponse[],
  agentId: string,
  userId: string,
): SessionResponse[] {
  return listed
    .filter(
      (session) =>
        session.agent_id === agentId &&
        session.session_mode === "persistent" &&
        session.caller_type === "user" &&
        session.caller_id === userId,
    )
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
}
