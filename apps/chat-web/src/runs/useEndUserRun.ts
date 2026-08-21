import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { api } from "../api/client";
import type { EndUserRunResponse } from "../api/types";

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
    queryFn: () => api<EndUserRunResponse>(`/api/v1/end-user/runs/${runId}`),
  };
}

export function useEndUserRun(runId: string | null): UseQueryResult<EndUserRunResponse> {
  return useQuery({
    ...endUserRunQueryOptions(runId ?? ""),
    enabled: runId !== null,
    refetchInterval: (query) => (query.state.data?.finished_at == null ? 1000 : false),
  });
}

/**
 * Plan §10's Run half: cancel a Run this end user started.
 *
 * `expected_state_version` is not optional and not read off anything this
 * module fetches on its own — the caller passes the version it last saw
 * (`ChatPage.tsx` reads it off the same `useEndUserRun` snapshot this
 * module produces), the same optimistic-concurrency contract the console's
 * own `/runs/{id}/cancel` uses. A stale value comes back as
 * `state_version_conflict` rather than cancelling a moment the caller never
 * actually saw.
 *
 * Deliberately the only control function here. §10 leaves pause and resume
 * unbuilt — see `ChatPage.tsx`'s own docstring for why — so there is no
 * `pauseEndUserRun`/`resumeEndUserRun` beside this one.
 */
export function cancelEndUserRun(
  runId: string,
  expectedStateVersion: number,
): Promise<EndUserRunResponse> {
  return api<EndUserRunResponse>(`/api/v1/end-user/runs/${runId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ expected_state_version: expectedStateVersion }),
  });
}
