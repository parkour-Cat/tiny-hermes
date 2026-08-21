import { useQueries, useQuery } from "@tanstack/react-query";

import { publishedIn, type ListedAgent } from "./published";
import { api } from "../api/client";
import type { AgentResponse, WorkspaceResponse } from "../api/types";

export function usePublishedAgents(): {
  rows: ListedAgent[];
  pending: boolean;
  error: unknown;
  refetch: () => void;
} {
  const workspaces = useQuery({
    queryKey: ["workspaces"] as const,
    queryFn: () => api<WorkspaceResponse[]>("/api/v1/workspaces"),
  });
  const listings = useQueries({
    queries: (workspaces.data ?? []).map((workspace) => ({
      queryKey: ["agents", workspace.id] as const,
      queryFn: () => api<AgentResponse[]>("/api/v1/agents", { workspace: workspace.id }),
    })),
  });
  const failed = [workspaces, ...listings].find((query) => query.isError);
  return {
    rows: publishedIn(
      workspaces.data ?? [],
      listings.map((query) => query.data),
    ),
    pending: workspaces.isPending || listings.some((query) => query.isPending),
    error: failed?.error,
    refetch: () => {
      void workspaces.refetch();
    },
  };
}
