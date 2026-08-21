import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { listEndUserApprovals } from "../api/approvals";
import type { ApprovalResponse } from "../api/types";

/**
 * Plan §10: what stopped a Run and is waiting on this end user, polled the
 * same way `useEndUserRun` polls a Run snapshot rather than opening a
 * second stream for it.
 *
 * `active` gates the poll on the caller rather than always running it —
 * `ChatPage.tsx` passes `run?.status === "waiting_approval"`, so this only
 * asks while the Run in front of the person is actually stopped for one,
 * not on every idle screen this app renders.
 */
export function useEndUserApprovals(active: boolean): UseQueryResult<ApprovalResponse[]> {
  return useQuery({
    queryKey: ["end-user-approvals"] as const,
    queryFn: listEndUserApprovals,
    enabled: active,
    refetchInterval: active ? 2000 : false,
  });
}
