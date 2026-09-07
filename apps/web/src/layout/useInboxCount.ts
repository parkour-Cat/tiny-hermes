import { useQueries } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ApprovalsPageResponse, ProposalResponse } from "../api/types";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useProposalAccess } from "../workspace/useProposalAccess";

/** Only queues this reader can act on count. A truncated result is a lower bound. */
export function useInboxCount(): number | string | null {
  const workspaceId = useWorkspaceId();
  const access = useProposalAccess();
  const queues = [
    { enabled: access.admin, path: "/api/v1/approvals?approval_type=governance_approval&limit=100", read: (body: unknown) => {
      const page = body as ApprovalsPageResponse;
      return { count: page.items.length, more: page.has_more, unknown: false };
    } },
    { enabled: access.writer, path: "/api/v1/skill-proposals?status=pending", read: (body: unknown) => {
      const proposals = body as ProposalResponse[];
      return { count: proposals.filter(access.canDecide).length, more: false, unknown: proposals.some(access.unresolved) };
    } },
    { enabled: access.admin, path: "/api/v1/memories/pending", read: (body: unknown) => ({ count: (body as unknown[]).length, more: false, unknown: false }) },
  ];
  const queries = useQueries({ queries: queues.map((queue) => ({
    queryKey: ["inbox-count", workspaceId, queue.path],
    queryFn: () => api<unknown>(queue.path, { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null && queue.enabled,
    retry: false,
    refetchInterval: 30_000,
  })) });
  if (access.loading) return null;
  if (access.unknown) return "?";
  const relevant = queries.filter((_, index) => queues[index]!.enabled);
  if (relevant.some((query) => query.isError)) return "?";
  if (relevant.some((query) => query.data === undefined)) return null;
  const counts = queues.flatMap((queue, index) => queue.enabled ? [queue.read(queries[index]!.data)] : []);
  if (counts.some((item) => item.unknown)) return "?";
  const count = counts.reduce((sum, item) => sum + item.count, 0);
  return counts.some((item) => item.more) ? `${count}+` : count;
}
