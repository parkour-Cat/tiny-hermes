import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, beforeEach } from "vitest";

import { RunDetailPage } from "./RunDetailPage";
import { moment } from "../i18n/moment";
import { t } from "../i18n/zh-CN";
import { server } from "../test/server";
import { TestTheme } from "../test/TestTheme";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const RUN = "44444444-5555-4666-8777-888888888888";
const OTHER_RUN = "55555555-6666-4777-8888-999999999999";
const SESSION = "33333333-4444-4555-8666-777777777777";
const VERSION = "66666666-7777-4888-8999-aaaaaaaaaaaa";

const BUDGET = {
  max_execution_seconds: 600,
  consumed_execution_ms: 1_200,
  max_elapsed_seconds: 3_600,
  elapsed_deadline_at: "2026-08-10T03:00:00Z",
  max_model_calls: 12,
  consumed_model_calls: 3,
  max_tool_calls: 7,
  consumed_tool_calls: 2,
  max_tokens: null,
  consumed_tokens: 480,
  max_derived_retries: 2,
  derived_retry_count: 0,
};

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN,
    session_id: SESSION,
    agent_version_id: VERSION,
    status: "running",
    state_version: 4,
    session_sequence: 1,
    blocked_by_run_id: null,
    pause_reason: null,
    wait_kind: null,
    wait_deadline_at: null,
    retry_of_run_id: null,
    budget_root_run_id: RUN,
    last_event_sequence: 2,
    queue: { position: 1, status: "head" },
    budget: BUDGET,
    available_actions: ["pause", "cancel"],
    checkpoint_replay_safe: true,
    checkpoint_effect_status: "none",
    created_at: "2026-08-10T02:00:00Z",
    started_at: "2026-08-10T02:00:05Z",
    finished_at: null,
    ...overrides,
  };
}

function frame(sequence: number, eventType: string): string {
  const data = JSON.stringify({
    sequence,
    event_type: eventType,
    occurred_at: "2026-08-10T02:00:07Z",
    payload: {},
  });
  return `id: ${sequence}\nevent: ${eventType}\ndata: ${data}\n\n`;
}

/**
 * The stream, left open.
 *
 * A live Run's stream does not end, and a test whose stream closes would send
 * the page reconnecting for reasons that have nothing to do with the test.
 */
function held(frames: string[] = []) {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const text of frames) {
        controller.enqueue(encoder.encode(text));
      }
    },
  });
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
}

function stream(frames: string[] = []): void {
  server.use(http.get(`/api/v1/runs/${RUN}/events`, () => held(frames)));
}

function renderRun(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/runs/${RUN}`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

beforeEach(() => {
  server.use(
    http.get(`/api/v1/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.get(`/api/v1/runs/${RUN}/artifacts`, () => HttpResponse.json([])),
  );
});

/** The 概要 card, so a value is read where the page actually states it. */
async function summary(): Promise<HTMLElement> {
  return (await screen.findByText(t("summarySection"))).closest(".ant-card") as HTMLElement;
}

test("概要 states the Run's status, its state version, its checkpoint, and every budget row", async () => {
  server.use(http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())));
  stream();

  renderRun();
  const card = within(await summary());

  expect(card.getByText("执行中")).toBeInTheDocument();
  expect(card.getByText("4")).toBeInTheDocument();
  expect(card.getByText(t("yes"))).toBeInTheDocument();
  expect(card.getByText("none")).toBeInTheDocument();
  // Milliseconds against a limit written in seconds: printed as seconds, or the
  // two numbers would read as one being two hundred times the other.
  expect(card.getByText("1.2 / 600")).toBeInTheDocument();
  expect(card.getByText("3 / 12")).toBeInTheDocument();
  expect(card.getByText("2 / 7")).toBeInTheDocument();
  expect(card.getByText(`480 / ${t("budgetUnlimited")}`)).toBeInTheDocument();
  expect(card.getByText("0 / 2")).toBeInTheDocument();
  expect(card.getByText(moment("2026-08-10T03:00:00Z"))).toBeInTheDocument();
});

test("the Runs this one descends from are links to them", async () => {
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(run({ retry_of_run_id: OTHER_RUN, budget_root_run_id: OTHER_RUN })),
    ),
  );
  stream();

  renderRun();
  const card = within(await summary());

  for (const link of await card.findAllByRole("link", { name: OTHER_RUN })) {
    expect(link).toHaveAttribute("href", `/workspaces/${WORKSPACE}/runs/${OTHER_RUN}`);
  }
  expect(card.getAllByRole("link", { name: OTHER_RUN })).toHaveLength(2);
});

test("the buttons are the actions the platform offers, and nothing else", async () => {
  // `running` would suggest 暂停 and 取消 to anyone reading the status. The
  // platform says only 暂停 is available, and the platform is the authority.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run({ available_actions: ["pause"] }))),
  );
  stream();

  renderRun();

  expect(await screen.findByRole("button", { name: t("pauseRun") })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: t("resumeRun") })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: t("cancelRun") })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: t("retryRun") })).not.toBeInTheDocument();
});

test("暂停 sends the state version it read, and reports a request rather than a pause", async () => {
  const bodies: unknown[] = [];
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.post(`/api/v1/runs/${RUN}/pause`, async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(run({ state_version: 5, available_actions: ["cancel"] }));
    }),
  );
  stream();

  renderRun();
  await userEvent.click(await screen.findByRole("button", { name: t("pauseRun") }));

  await waitFor(() => expect(bodies).toEqual([{ expected_state_version: 4 }]));
  // The Run is still `running` until the worker reaches a checkpoint. Saying
  // 已暂停 here would teach the user something the platform has not done.
  expect(await screen.findByText(t("pauseRequested"))).toBeInTheDocument();
});

test("取消任务 sends nothing until the question is answered", async () => {
  let cancels = 0;
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.post(`/api/v1/runs/${RUN}/cancel`, () => {
      cancels += 1;
      return HttpResponse.json(run({ status: "cancelling", available_actions: [] }));
    }),
  );
  stream();

  renderRun();
  await userEvent.click(await screen.findByRole("button", { name: t("cancelRun") }));

  expect(await screen.findByText(t("cancelRunWarning"))).toBeInTheDocument();
  expect(cancels).toBe(0);

  await userEvent.click(screen.getByRole("button", { name: t("confirm") }));
  await waitFor(() => expect(cancels).toBe(1));
  expect(await screen.findByText(t("cancelRequested"))).toBeInTheDocument();
});

test("a Run that moved on is reported, and the buttons come back matching where it is now", async () => {
  let reads = 0;
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => {
      reads += 1;
      return HttpResponse.json(
        reads === 1 ? run() : run({ status: "paused", state_version: 5, available_actions: ["resume"] }),
      );
    }),
    http.post(`/api/v1/runs/${RUN}/pause`, () =>
      HttpResponse.json(
        { code: "state_version_conflict", detail: "The run changed while you were reading it." },
        { status: 409 },
      ),
    ),
  );
  stream();

  renderRun();
  await userEvent.click(await screen.findByRole("button", { name: t("pauseRun") }));

  // Refetched rather than resent: nothing the user typed is at stake, so the
  // console shows them where the Run actually is and lets them decide again.
  expect(await screen.findByText(t("stateVersionConflict"))).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: t("resumeRun") })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: t("pauseRun") })).not.toBeInTheDocument();
});

test("an event re-reads the snapshot, because only the state machine knows the state", async () => {
  let reads = 0;
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => {
      reads += 1;
      return HttpResponse.json(
        reads === 1 ? run() : run({ status: "completed", finished_at: "2026-08-10T02:01:00Z" }),
      );
    }),
  );
  stream([frame(3, "run_completed")]);

  renderRun();

  expect(await screen.findByText("已完成")).toBeInTheDocument();
  expect(reads).toBeGreaterThan(1);
});

test("时间线 lists each event and admits the history it cannot retrieve", async () => {
  let subscriptions = 0;
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.get(`/api/v1/runs/${RUN}/events`, () => {
      subscriptions += 1;
      return subscriptions === 1
        ? HttpResponse.json(
            {
              code: "event_cursor_too_old",
              detail: "Re-read the run snapshot before resubscribing to its events.",
              context: { earliest_available_sequence: 9, run_url: `/api/v1/runs/${RUN}` },
            },
            { status: 410 },
          )
        : held([frame(9, "run_slice_ended"), frame(10, "run_completed")]);
    }),
  );

  renderRun();
  const timeline = within((await screen.findByText(t("timelineSection"))).closest(
    ".ant-card",
  ) as HTMLElement);

  expect(
    await timeline.findByText(`${t("eventGapPrefix")}8${t("eventGapSuffix")}`),
  ).toBeInTheDocument();
  expect(await timeline.findByText("run_slice_ended")).toBeInTheDocument();
  expect(timeline.getByText("run_completed")).toBeInTheDocument();
  expect(timeline.getAllByText(moment("2026-08-10T02:00:07Z"))).toHaveLength(2);
  expect(timeline.getByText("#9")).toBeInTheDocument();
});

test("what the platform cannot produce is absent, not empty", async () => {
  server.use(http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())));
  stream();

  renderRun();
  await screen.findByText(t("summarySection"));

  // A pane that says "no data" reads as "nothing happened". These are M2/M3;
  // the console does not stand in for them. 产物 is the Files card now.
  for (const absent of ["父子任务树", "上下文和压缩事件", "Token 和费用"]) {
    expect(screen.queryByText(absent)).not.toBeInTheDocument();
  }
});

test("the session transcript and the run's files are listed", async () => {
  const artifact = {
    id: "77777777-8888-4999-8aaa-bbbbbbbbbbbb",
    run_id: RUN,
    session_id: SESSION,
    filename: "stdout.log",
    media_type: "text/plain",
    size_bytes: 12,
    sha256: "abc",
    truncated: false,
    expires_at: "2026-08-11T00:00:00Z",
  };
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.get(`/api/v1/sessions/${SESSION}/messages`, () =>
      HttpResponse.json([
        { role: "user", parts: [{ type: "text", text: "List the files." }] },
        {
          role: "assistant",
          parts: [
            {
              type: "tool_call",
              call_id: "c1",
              name: "file.list",
              arguments: { path: "." },
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              type: "tool_result",
              call_id: "c1",
              output: `ok\nartifact_id=${artifact.id}`,
              exit_code: 0,
              failed: false,
            },
          ],
        },
      ]),
    ),
    http.get(`/api/v1/runs/${RUN}/artifacts`, () => HttpResponse.json([artifact])),
  );
  stream();

  renderRun();

  expect(await screen.findByText("List the files.")).toBeInTheDocument();
  expect(screen.getByText("file.list")).toBeInTheDocument();
  expect(screen.getByText("stdout.log")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: t("downloadArtifact") }).length).toBeGreaterThan(0);
});

test("a timeline event keeps its payload folded", async () => {
  server.use(http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())));
  stream([
    `id: 3\nevent: run_completed\ndata: ${JSON.stringify({
      sequence: 3,
      event_type: "run_completed",
      occurred_at: "2026-08-10T02:00:07Z",
      payload: { reason: "goal" },
    })}\n\n`,
  ]);

  renderRun();

  expect(await screen.findByText("run_completed")).toBeInTheDocument();
  const payload = screen.getByText(t("eventPayload")).closest("details");
  expect(payload).not.toBeNull();
  expect(payload).not.toHaveAttribute("open");
});
