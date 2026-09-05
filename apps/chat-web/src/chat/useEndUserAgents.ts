import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { EndUserAgentResponse } from "../api/types";

/**
 * The Agents this session's credential may open, with their names.
 *
 * Read from `GET /api/v1/end-user/agents`, which runs §5's two gates over
 * the credential's own `agents` claim — never a workspace listing. This is
 * what lets the title say 「客服 Concierge」 instead of `concierge-compare`,
 * and what the switch menu offers. An end user with one Agent sees no menu.
 */
export function useEndUserAgents() {
  return useQuery({
    queryKey: ["end-user-agents"] as const,
    queryFn: () => api<EndUserAgentResponse[]>("/api/v1/end-user/agents"),
    retry: false,
    staleTime: 60_000,
  });
}

export function agentName(agents: EndUserAgentResponse[] | undefined, alias: string): string {
  return agents?.find((agent) => agent.alias === alias)?.name ?? alias;
}
