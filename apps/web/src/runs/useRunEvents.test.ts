import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type ReactNode } from "react";
import { expect, test } from "vitest";

import { useRunEvents } from "./useRunEvents";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const RUN = "44444444-5555-4666-8777-888888888888";

const EVENTS_PATH = `/api/v1/runs/${RUN}/events`;
const SNAPSHOT_PATH = `/api/v1/runs/${RUN}`;

/** Long enough that a reconnection would have happened: the first backoff is 1s. */
const PAST_THE_FIRST_BACKOFF = 1_100;

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client }, children);
}

function frame(sequence: number, eventType: string): string {
  const data = JSON.stringify({
    sequence,
    event_type: eventType,
    occurred_at: "2026-08-10T02:00:00+00:00",
    payload: {},
  });
  return `id: ${sequence}\nevent: ${eventType}\ndata: ${data}\n\n`;
}

/** An event-stream response; `hold` leaves the connection open, as a live Run does. */
function sse(frames: string[], hold = false) {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const text of frames) {
        controller.enqueue(encoder.encode(text));
      }
      if (!hold) {
        controller.close();
      }
    },
  });
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
}

function runSnapshot(finished: boolean) {
  return {
    id: RUN,
    session_id: "33333333-4444-4555-8666-777777777777",
    agent_version_id: "66666666-7777-4888-8999-aaaaaaaaaaaa",
    status: finished ? "completed" : "running",
    state_version: 3,
    session_sequence: 1,
    blocked_by_run_id: null,
    pause_reason: null,
    wait_kind: null,
    wait_deadline_at: null,
    retry_of_run_id: null,
    budget_root_run_id: RUN,
    parent_run_id: null,
    depth: 0,
    children: [],
    last_event_sequence: 2,
    queue: { position: 1, status: finished ? "terminal" : "head" },
    budget: {
      max_execution_seconds: 600,
      consumed_execution_ms: 1200,
      max_elapsed_seconds: 3600,
      elapsed_deadline_at: "2026-08-10T03:00:00Z",
      max_model_calls: 12,
      consumed_model_calls: 1,
      max_tool_calls: 7,
      consumed_tool_calls: 0,
      max_tokens: null,
      consumed_tokens: 0,
      max_derived_retries: 2,
      derived_retry_count: 0,
    },
    available_actions: [],
    checkpoint_replay_safe: true,
    checkpoint_effect_status: "none",
    goal: { round: null, outcome: null, unmet: [] },
    created_at: "2026-08-10T02:00:00Z",
    started_at: "2026-08-10T02:00:05Z",
    finished_at: finished ? "2026-08-10T02:01:00Z" : null,
  };
}

function sequences(entries: ReturnType<typeof useRunEvents>["entries"]): number[] {
  return entries.flatMap((entry) => (entry.kind === "event" ? [entry.frame.sequence] : []));
}

async function rest(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, PAST_THE_FIRST_BACKOFF));
}

test("events arrive in order, and a finished Run ends the subscription", async () => {
  const scopes: (string | null)[] = [];
  let calls = 0;
  server.use(
    http.get(EVENTS_PATH, ({ request }) => {
      calls += 1;
      scopes.push(new URL(request.url).searchParams.get("workspace_id"));
      return sse([frame(1, "run_created"), frame(2, "run_completed")]);
    }),
    http.get(SNAPSHOT_PATH, () => HttpResponse.json(runSnapshot(true))),
  );

  const { result } = renderHook(() => useRunEvents({ runId: RUN, workspaceId: WORKSPACE }), {
    wrapper,
  });

  await waitFor(() => expect(sequences(result.current.entries)).toEqual([1, 2]));
  await rest();
  // The workspace travels as a query parameter because the stream route reads
  // it there — the one place in the API where a header would be ignored.
  expect(scopes).toEqual([WORKSPACE]);
  expect(calls).toBe(1);
  expect(result.current.error).toBeNull();
});

test("a stream that ends early resumes from the last sequence seen, delivering nothing twice", async () => {
  const cursors: (string | null)[] = [];
  server.use(
    http.get(EVENTS_PATH, ({ request }) => {
      cursors.push(request.headers.get("Last-Event-ID"));
      return cursors.length === 1
        ? sse([frame(1, "run_created")])
        : sse([frame(2, "run_completed")]);
    }),
    http.get(SNAPSHOT_PATH, () => HttpResponse.json(runSnapshot(cursors.length > 1))),
  );

  const { result } = renderHook(() => useRunEvents({ runId: RUN, workspaceId: WORKSPACE }), {
    wrapper,
  });

  await waitFor(() => expect(sequences(result.current.entries)).toEqual([1, 2]), {
    timeout: 4_000,
  });
  expect(cursors).toEqual([null, "1"]);
});

test("an expired cursor marks the gap it leaves and resubscribes from what survives", async () => {
  const cursors: (string | null)[] = [];
  server.use(
    http.get(EVENTS_PATH, ({ request }) => {
      cursors.push(request.headers.get("Last-Event-ID"));
      return cursors.length === 1
        ? HttpResponse.json(
            {
              code: "event_cursor_too_old",
              detail: "Re-read the run snapshot before resubscribing to its events.",
              context: { earliest_available_sequence: 51, run_url: SNAPSHOT_PATH },
            },
            { status: 410 },
          )
        : sse([frame(51, "run_completed")]);
    }),
    http.get(SNAPSHOT_PATH, () => HttpResponse.json(runSnapshot(true))),
  );

  const { result } = renderHook(() => useRunEvents({ runId: RUN, workspaceId: WORKSPACE }), {
    wrapper,
  });

  await waitFor(() => expect(result.current.entries).toHaveLength(2));
  expect(result.current.entries[0]).toEqual({ kind: "gap", missing: 50, after: 0 });
  expect(sequences(result.current.entries)).toEqual([51]);
  expect(cursors).toEqual([null, "50"]);
});

test("a refused stream stops the reader and is not tried again", async () => {
  let calls = 0;
  server.use(
    http.get(EVENTS_PATH, () => {
      calls += 1;
      return HttpResponse.json(
        { code: "forbidden", detail: "Not a member of this workspace." },
        { status: 403 },
      );
    }),
  );

  const { result } = renderHook(() => useRunEvents({ runId: RUN, workspaceId: WORKSPACE }), {
    wrapper,
  });

  // The code, not a sentence. This hook decides *what* happened and the
  // page decides how to say it, so asserting the wording here would tie a
  // data test to a locale — and would have kept passing while the console's
  // language switcher did nothing to it.
  await waitFor(() => expect(result.current.error?.code).toBe("forbidden"));
  await rest();
  expect(calls).toBe(1);
});

test("a Run the platform does not know stops the reader and is not tried again", async () => {
  let calls = 0;
  server.use(
    http.get(EVENTS_PATH, () => {
      calls += 1;
      return HttpResponse.json({ code: "run_not_found", detail: "No such run." }, { status: 404 });
    }),
  );

  const { result } = renderHook(() => useRunEvents({ runId: RUN, workspaceId: WORKSPACE }), {
    wrapper,
  });

  await waitFor(() => expect(result.current.error?.code).toBe("run_not_found"));
  await rest();
  expect(calls).toBe(1);
});

test("leaving the page aborts the connection instead of holding it open", async () => {
  let signal: AbortSignal | null = null;
  server.use(
    http.get(EVENTS_PATH, ({ request }) => {
      signal = request.signal;
      return sse([frame(1, "run_created")], true);
    }),
  );

  const { result, unmount } = renderHook(
    () => useRunEvents({ runId: RUN, workspaceId: WORKSPACE }),
    { wrapper },
  );

  await waitFor(() => expect(sequences(result.current.entries)).toEqual([1]));
  unmount();

  await waitFor(() => expect(signal?.aborted).toBe(true));
});
