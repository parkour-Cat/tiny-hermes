import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { api } from "../api/client";
import type { RunResponse } from "../api/types";

/**
 * An end user's window onto their own Run: polling, not `useRunEvents`'s
 * SSE subscription.
 *
 * `GET /api/v1/end-user/runs/{id}/events` does not exist — the design gap
 * this task's report flags is that no read route existed at all until this
 * task added the snapshot the console's own SSE stream refreshes off of
 * (`runQueryOptions` in `useRunEvents.ts`). A snapshot polled every second
 * is a smaller thing to have built than a second event-stream consumer, and
 * an end user's own conversation is one Run at a time, never the fleet a
 * console operator watches — the cost this trades away (a live token-by-
 * token stream) is a real reduction from what the console gets, not a
 * hidden one.
 */
export function endUserRunQueryOptions(runId: string) {
  return {
    queryKey: ["end-user-run", runId] as const,
    queryFn: () => api<RunResponse>(`/api/v1/end-user/runs/${runId}`),
  };
}

export function useEndUserRun(runId: string | null): UseQueryResult<RunResponse> {
  return useQuery({
    ...endUserRunQueryOptions(runId ?? ""),
    enabled: runId !== null,
    refetchInterval: (query) => (query.state.data?.finished_at == null ? 1000 : false),
  });
}
