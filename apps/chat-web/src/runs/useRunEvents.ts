import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { readFrames } from "./eventFrames";
import { ApiError, api, asApiError } from "../api/client";
import { problemMessage } from "../api/messages";
import type { RunEventFrame, RunResponse } from "../api/types";

/**
 * The console's subscription to a Run's event stream.
 *
 * This is `fetch` and a stream reader rather than `EventSource`, which is the
 * obvious tool and cannot do the job: on a non-200 `EventSource` fires `error`
 * and closes without exposing the status or the body, so the `410`'s
 * `earliest_available_sequence` — the only thing that says how much of the
 * timeline is gone — would be unreadable, and the console would show a
 * truncated history that looks complete. Reconnection is the price: it is
 * written here instead of being inherited.
 *
 * Two details follow the stream route rather than the rest of the API. The
 * workspace goes in the query string, because that route reads it there and
 * would answer `workspace_required` to the `X-Workspace-Id` header every other
 * request uses; the cursor goes in `Last-Event-ID`, which is the header that
 * route reads.
 */

/** How long to wait before reconnecting, after 1, 2, 3… quiet failures. */
function backoffMilliseconds(failures: number): number {
  return Math.min(1_000 * 2 ** (failures - 1), 10_000);
}

/**
 * One line of the timeline: an event, or an admission that events are missing.
 *
 * The gap is a first-class entry rather than a flag on the next event because
 * resynchronizing quietly produces a timeline that reads as complete and is
 * not, which is the same failure as inventing data with better manners.
 */
export type TimelineEntry =
  | { kind: "event"; frame: RunEventFrame }
  | { kind: "gap"; missing: number; after: number };

export type RunEvents = {
  entries: TimelineEntry[];
  /** Why the subscription stopped, when it stopped for a reason worth showing. */
  error: string | null;
};

/**
 * The Run snapshot, defined once.
 *
 * The stream owns the timeline and this owns everything else on the page. They
 * share a definition so that "refetch the snapshot after an event" and "the
 * snapshot the page is showing" cannot become two different requests.
 */
export function runQueryOptions(workspaceId: string, runId: string) {
  return {
    queryKey: ["run", workspaceId, runId] as const,
    queryFn: () => api<RunResponse>(`/api/v1/runs/${runId}`, { workspace: workspaceId }),
  };
}

export function useRunEvents({
  runId,
  workspaceId,
}: {
  runId: string | null;
  workspaceId: string | null;
}): RunEvents {
  const queryClient = useQueryClient();
  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (runId === null || workspaceId === null) {
      return;
    }
    const controller = new AbortController();
    const { signal } = controller;
    setEntries([]);
    setError(null);

    /** Whether the state machine says this Run is over, which only it can say. */
    const hasFinished = async (): Promise<boolean> => {
      try {
        const run = await queryClient.fetchQuery(runQueryOptions(workspaceId, runId));
        return run.finished_at !== null;
      } catch {
        // A snapshot that cannot be read is not evidence the Run ended.
        return false;
      }
    };

    /**
     * Re-reads the snapshot, even while the page is still on its first read.
     *
     * TanStack cannot force a refetch of a query that has never resolved: it
     * hands back the request already in flight, which was sent before this
     * event existed and may answer with a state older than the event just
     * shown. When that is what happened, the snapshot is asked for a second
     * time once the first read has landed.
     */
    const refresh = async (): Promise<void> => {
      const { queryKey } = runQueryOptions(workspaceId, runId);
      const before = queryClient.getQueryState(queryKey);
      const joined = before?.fetchStatus === "fetching" && before.dataUpdatedAt === 0;
      await queryClient.invalidateQueries({ queryKey, exact: true }, { cancelRefetch: true });
      if (joined && !signal.aborted) {
        await queryClient.invalidateQueries({ queryKey, exact: true }, { cancelRefetch: true });
      }
    };

    /** Reads one connection to its end; returns the cursor and what it moved. */
    const readStream = async (response: Response, from: number): Promise<[number, number]> => {
      let cursor = from;
      let received = 0;
      if (response.body === null) {
        return [cursor, received];
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) {
          return [cursor, received];
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        const read = readFrames(buffer);
        buffer = read.rest;
        // A frame at or before the cursor would be one the reader already
        // showed. The server does not resend, and a timeline that repeats
        // itself after a reconnection is worse than one that waits.
        const fresh = read.frames.filter((frame) => frame.sequence > cursor);
        if (fresh.length === 0) {
          continue;
        }
        cursor = fresh.reduce((highest, frame) => Math.max(highest, frame.sequence), cursor);
        received += fresh.length;
        setEntries((current) => [
          ...current,
          ...fresh.map((frame): TimelineEntry => ({ kind: "event", frame })),
        ]);
        // The snapshot is how the state machine speaks, so 概要 and the
        // available actions are re-read rather than guessed at from the event.
        void refresh();
      }
    };

    const subscribe = async (): Promise<void> => {
      let cursor = 0;
      let failures = 0;
      while (!signal.aborted) {
        let moved = false;
        try {
          // No cursor on a first subscription: the stream reads a missing
          // `Last-Event-ID` as "from the beginning", which is what is wanted,
          // and sending `0` would say the same thing less clearly.
          const headers = new Headers();
          if (cursor > 0) {
            headers.set("Last-Event-ID", String(cursor));
          }
          const response = await fetch(
            `/api/v1/runs/${runId}/events?workspace_id=${encodeURIComponent(workspaceId)}`,
            { credentials: "include", headers, signal },
          );
          if (!response.ok) {
            const problem = await asApiError(response);
            if (problem.status === 410) {
              cursor = resynchronize(problem, cursor, setEntries);
              failures = 0;
              continue;
            }
            // Anything else the platform refuses outright — no membership, no
            // such Run, a malformed request — is not fixed by asking again.
            if (problem.status < 500) {
              setError(problemMessage(problem));
              return;
            }
            throw problem;
          }
          const [next, received] = await readStream(response, cursor);
          cursor = next;
          moved = received > 0;
        } catch {
          if (signal.aborted) {
            return;
          }
        }
        if (signal.aborted || (await hasFinished())) {
          return;
        }
        // Progress resets the backoff: a connection that delivered events and
        // then dropped is a different thing from a server refusing to talk.
        failures = moved ? 1 : failures + 1;
        await sleep(backoffMilliseconds(failures), signal);
      }
    };

    void subscribe();
    return () => controller.abort();
  }, [runId, workspaceId, queryClient]);

  return { entries, error };
}

/**
 * Records how much history the platform has dropped, and where to resume.
 *
 * The stream's cursor is exclusive, so subscribing from
 * `earliest_available_sequence - 1` asks for the oldest event still kept.
 */
function resynchronize(
  problem: ApiError,
  cursor: number,
  setEntries: (update: (current: TimelineEntry[]) => TimelineEntry[]) => void,
): number {
  const earliest = Number(problem.context.earliest_available_sequence);
  if (!Number.isFinite(earliest) || earliest <= cursor + 1) {
    return cursor;
  }
  const missing = earliest - 1 - cursor;
  setEntries((current) => [...current, { kind: "gap", missing, after: cursor }]);
  return earliest - 1;
}

/** A wait that ends early when the subscriber leaves. */
function sleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener("abort", finish, { once: true });
    function finish(): void {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
  });
}
