import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ProposalResponse, SkillResponse } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useMyRole } from "./useMyRole";
import { useWorkspaceId } from "./useWorkspaceId";

/** A proposal's scan result does not grant the reader permission to publish it. */
export function useProposalAccess() {
  const { user } = useAuth();
  const { role, loading } = useMyRole();
  const workspaceId = useWorkspaceId();
  const platform = user?.is_platform_admin === true || role === "platform_admin";
  const admin = platform || role === "workspace_admin";
  const writer = admin || role === "developer";
  const skills = useQuery({
    queryKey: ["skills", workspaceId],
    queryFn: () => api<SkillResponse[]>("/api/v1/skills", { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null && writer && !platform,
  });
  const scopeOf = (proposal: ProposalResponse) => proposal.skill_id === null ? "workspace" : skills.data?.find((skill) => skill.id === proposal.skill_id)?.scope;
  return {
    admin, writer,
    loading: !platform && (loading || (writer && skills.isPending)),
    unknown: !platform && ((!loading && role === null) || (writer && skills.isError)),
    unresolved: (proposal: ProposalResponse) => writer && !platform && scopeOf(proposal) === undefined,
    canDecide: (proposal: ProposalResponse) => platform || (writer && scopeOf(proposal) === "workspace"),
  };
}
