import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, beforeEach } from "vitest";

import { RunDetailPage } from "./RunDetailPage";
import { moment } from "../i18n/moment";
import { t } from "../i18n/zh-CN";
import { fill } from "../runs/explain";
import { server } from "../test/server";
import { TestTheme } from "../test/TestTheme";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const RUN = "44444444-5555-4666-8777-888888888888";
const OTHER_RUN = "55555555-6666-4777-8888-999999999999";
const THIRD_RUN = "66666666-7777-4888-8999-aaaaaaaaaaaa";
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
  max_cost: null,
  cost_currency: "USD",
  consumed_cost: "0.004500",
  cost_quality: "provider",
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
    parent_run_id: null,
    depth: 0,
    children: [],
    last_event_sequence: 2,
    queue: { position: 1, status: "head" },
    budget: BUDGET,
    available_actions: ["pause", "cancel"],
    checkpoint_replay_safe: true,
    checkpoint_effect_status: "none",
    goal: { round: null, outcome: null, unmet: [], preempted: false },
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

  expect(card.getByText("running")).toBeInTheDocument();
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

test("a preempted Run says so in a banner and next to its goal outcome", async () => {
  // MINOR (review): the earlier fix only asserted at `statusNote`, the
  // rendering-decision layer, not the component — this is the layer this
  // repo keeps losing things at, so this test renders the real page.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(
        run({
          status: "completed",
          goal: { round: 3, outcome: "continue", unmet: ["pytest -q"], preempted: true },
        }),
      ),
    ),
  );
  stream();

  renderRun();

  expect(await screen.findByText(t("runPreemptedNote"))).toBeInTheDocument();
  const card = within(await summary());
  expect(card.getByText(t("goalOutcomeContinue"), { exact: false })).toBeInTheDocument();
  expect(card.getByText(t("runGoalPreemptedSuffix"), { exact: false })).toBeInTheDocument();
});

test("a Run that actually finished gets no preempted banner", async () => {
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(
        run({
          status: "completed",
          goal: { round: 2, outcome: "done", unmet: [], preempted: false },
        }),
      ),
    ),
  );
  stream();

  renderRun();
  await summary();

  expect(screen.queryByText(t("runPreemptedNote"))).not.toBeInTheDocument();
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

  expect(await screen.findByText("completed")).toBeInTheDocument();
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
  // the console does not stand in for them. 产物 is the Files card now, and
  // context and compaction are sentences on the timeline rather than a pane —
  // a Run that trimmed nothing should say nothing, which is this case.
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

test("shows a tool turn in the transcript rather than an empty row", async () => {
  // Reported as "why is tool empty?" — and a blank row reads exactly like a
  // bug in the page. The tools section below always carried this content;
  // the transcript is where the step between the assistant's stated intent
  // and its conclusion had gone missing.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.get(`/api/v1/sessions/${SESSION}/messages`, () =>
      HttpResponse.json([
        { role: "assistant", parts: [{ type: "text", text: "先看看目录。" }] },
        {
          role: "assistant",
          parts: [
            { type: "tool_call", call_id: "c9", name: "file.list", arguments: { path: "." } },
          ],
        },
        {
          role: "tool",
          parts: [
            { type: "tool_result", call_id: "c9", output: "3 entries", failed: false },
          ],
        },
      ]),
    ),
    http.get(`/api/v1/runs/${RUN}/artifacts`, () => HttpResponse.json([])),
  );
  stream();

  renderRun();

  expect(await screen.findByText("先看看目录。")).toBeInTheDocument();
  // The arrows only appear in a transcript line, never in the tools section,
  // so matching them pins the assertion to the row that used to be blank.
  expect(screen.getByText(/→ file\.list/)).toBeInTheDocument();
  expect(screen.getByText(/← 3 entries/)).toBeInTheDocument();
});

test("a withdrawn turn is marked as withdrawn instead of reading as a live one", async () => {
  // `list_session_messages` deliberately does not filter withdrawn rows: the
  // transcript is what a person reads, and dropping the row would tell them
  // they never said it. That only works if the page says which row it is —
  // otherwise the transcript claims a message is still in play when the model
  // will never see it again.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.get(`/api/v1/sessions/${SESSION}/messages`, () =>
      HttpResponse.json([
        { role: "user", parts: [{ type: "text", text: "第一句" }], withdrawn_at: null },
        {
          role: "user",
          parts: [{ type: "text", text: "第二句" }],
          withdrawn_at: "2026-08-26T02:00:00Z",
        },
      ]),
    ),
    http.get(`/api/v1/runs/${RUN}/artifacts`, () => HttpResponse.json([])),
  );
  stream();

  renderRun();

  // Anchored on the row carrying the text, not on an index into the list.
  const withdrawn = (await screen.findByText("第二句")).closest("article");
  const kept = screen.getByText("第一句").closest("article");
  expect(withdrawn).not.toBeNull();
  expect(within(withdrawn as HTMLElement).getByText(t("withdrawnTurn"))).toBeInTheDocument();
  // The other direction too: a page that tagged every row would pass the first
  // assertion and still be wrong.
  expect(within(kept as HTMLElement).queryByText(t("withdrawnTurn"))).toBeNull();
});

test("a trimmed context is said in words, not left as a payload to decode", async () => {
  // The one class of event that reports a decision nobody asked for: the round
  // was sent something other than what the transcript holds. `context_trimmed`
  // and a JSON blob leave a reader wondering what was lost — nothing was, and
  // the sentence is where that is said.
  server.use(http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())));
  stream([
    `id: 3\nevent: context_trimmed\ndata: ${JSON.stringify({
      sequence: 3,
      event_type: "context_trimmed",
      occurred_at: "2026-08-10T02:00:07Z",
      payload: {
        segment: "old_tool_results",
        dropped: 2,
        freed_estimate: 9000,
        references: ["c1", "c2"],
      },
    })}\n\n`,
  ]);

  renderRun();
  const timeline = within((await screen.findByText(t("timelineSection"))).closest(
    ".ant-card",
  ) as HTMLElement);

  const said = fill(t("contextTrimmedOldToolResults"), { dropped: "2", freed: "9000" });
  expect(await timeline.findByText(said)).toBeInTheDocument();
  // Still folded underneath it: the sentence is the reading, the payload is the
  // record, and neither replaces the other.
  expect(timeline.getByText(t("eventPayload")).closest("details")).not.toHaveAttribute("open");
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

test("the cost is shown with where the number came from", async () => {
  server.use(http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())));
  stream();

  renderRun();

  // The provenance is always shown, never only when it is bad: a figure whose
  // origin appears sometimes is one a reader stops looking for.
  expect(await screen.findByText("0.004500 USD / 不限")).toBeInTheDocument();
  expect(screen.getByText("来自服务商")).toBeInTheDocument();
});

test("a Run whose endpoint has no price says unknown, never zero", async () => {
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(
        run({
          budget: { ...BUDGET, consumed_cost: null, cost_quality: "unknown" },
        }),
      ),
    ),
  );
  stream();

  renderRun();

  // §12.4 as a person sees it. A console that showed `0` for both an unpriced
  // endpoint and one priced at nothing would be where the distinction died.
  expect(await screen.findByText("未知")).toBeInTheDocument();
  expect(screen.getByText("未配置价格")).toBeInTheDocument();
});

test("a Run that delegated shows its children and each one is a link", async () => {
  // A status beside each, because "two children, one of which failed" is the
  // shape of the question a person opens this page with. Drawn from the tree
  // route rather than `children` on the Run: the same card has to work when
  // it is a *child* that was opened, and a child's `children` is empty.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(
        run({
          status: "waiting_external",
          wait_kind: "child_runs",
          children: [
            { id: OTHER_RUN, status: "completed" },
            { id: THIRD_RUN, status: "running" },
          ],
        }),
      ),
    ),
    http.get(`/api/v1/runs/${RUN}/tree`, () =>
      HttpResponse.json({
        budget_root_run_id: RUN,
        budget: BUDGET,
        nodes: [
          { id: RUN, status: "waiting_external", depth: 0, parent_run_id: null, relation: "root", created_at: "2026-08-10T02:00:00Z", finished_at: null },
          { id: OTHER_RUN, status: "completed", depth: 1, parent_run_id: RUN, relation: "child", created_at: "2026-08-10T02:01:00Z", finished_at: null },
          { id: THIRD_RUN, status: "running", depth: 1, parent_run_id: RUN, relation: "child", created_at: "2026-08-10T02:01:00Z", finished_at: null },
        ],
      }),
    ),
  );
  stream();

  renderRun();

  const link = await screen.findByRole("link", { name: OTHER_RUN });
  expect(link).toHaveAttribute("href", `/workspaces/${WORKSPACE}/runs/${OTHER_RUN}`);
  expect(screen.getByRole("link", { name: THIRD_RUN })).toBeInTheDocument();
  // And the note that says nobody is being held up by this.
  expect(screen.getByText(t("waitingChildRunsNote"))).toBeInTheDocument();
});

test("a delegated Run says who delegated it", async () => {
  // A child holds a Session of its own, so without this the page gives no
  // indication at all that somebody else asked for this work.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(run({ parent_run_id: OTHER_RUN, depth: 1 })),
    ),
  );
  stream();

  renderRun();
  const card = within(await summary());

  expect(card.getByText(t("runParent"))).toBeInTheDocument();
  expect(card.getByRole("link", { name: OTHER_RUN })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/runs/${OTHER_RUN}`,
  );
});

test("an ordinary Run shows no tree at all", async () => {
  // Most Runs are alone in their tree, and a card saying so on every Run
  // detail page would be a permanent reminder of a feature nobody used.
  //
  // The tree route is stubbed with the one node it really returns, so this
  // tests the page's decision rather than a request that happened to fail.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () => HttpResponse.json(run())),
    http.get(`/api/v1/runs/${RUN}/tree`, () =>
      HttpResponse.json({
        budget_root_run_id: RUN,
        budget: BUDGET,
        nodes: [
          { id: RUN, status: "running", depth: 0, parent_run_id: null, relation: "root", created_at: "2026-08-10T02:00:00Z", finished_at: null },
        ],
      }),
    ),
  );
  stream();

  renderRun();
  await summary();

  expect(screen.queryByText(t("taskTreeSection"))).not.toBeInTheDocument();
  expect(screen.queryByText(t("runParent"))).not.toBeInTheDocument();
  // And no note claiming the budget is shared, because it is not.
  expect(screen.queryByText(t("budgetSharedNote"))).not.toBeInTheDocument();
});

const SIBLING = "77777777-1111-4222-8333-444444444444";
const PARENT = "66666666-1111-4222-8333-444444444444";

function tree(nodes: object[]) {
  return http.get(`/api/v1/runs/${RUN}/tree`, () =>
    HttpResponse.json({ budget_root_run_id: PARENT, nodes, budget: BUDGET }),
  );
}

test("a child Run can see the siblings it was delegated alongside", async () => {
  // §952's 完整父子任务树. Before the tree route, a child carried
  // `parent_run_id` and nothing else — so somebody who opened the child that
  // failed had no way to learn that three siblings succeeded without walking
  // up to the parent and back down by hand.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(run({ parent_run_id: PARENT, depth: 1, budget_root_run_id: PARENT })),
    ),
    tree([
      { id: PARENT, status: "completed", depth: 0, parent_run_id: null, relation: "root", created_at: "2026-08-10T02:00:00Z", finished_at: "2026-08-10T02:09:00Z" },
      { id: RUN, status: "running", depth: 1, parent_run_id: PARENT, relation: "child", created_at: "2026-08-10T02:01:00Z", finished_at: null },
      { id: SIBLING, status: "failed", depth: 1, parent_run_id: PARENT, relation: "child", created_at: "2026-08-10T02:01:00Z", finished_at: "2026-08-10T02:03:00Z" },
    ]),
  );

  renderRun();

  const section = (await screen.findByText(t("taskTreeSection"))).closest(".ant-card") as HTMLElement;
  expect(within(section).getByRole("link", { name: SIBLING })).toBeVisible();
  // And the Run being read is marked, because a tree of ids all looking alike
  // does not tell you which one you opened.
  expect(within(section).getByText(t("taskTreeYouAreHere"))).toBeVisible();
});

test("the budget is labelled as the tree's, because that is whose it is", async () => {
  // `budget` on a Run response is read from `run_budget_scopes` by
  // `budget_root_run_id` — it is the whole tree's consumption on every node.
  // A child's page heading it "Budget" tells an administrator this child made
  // the calls the tree made.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(run({ parent_run_id: PARENT, depth: 1, budget_root_run_id: PARENT })),
    ),
    tree([
      { id: PARENT, status: "completed", depth: 0, parent_run_id: null, relation: "root", created_at: "2026-08-10T02:00:00Z", finished_at: null },
      { id: RUN, status: "running", depth: 1, parent_run_id: PARENT, relation: "child", created_at: "2026-08-10T02:01:00Z", finished_at: null },
    ]),
  );

  renderRun();

  expect(await screen.findByText(t("budgetSharedNote"))).toBeVisible();
});


test("a Run stopped by its ceiling offers the one action that can move it", async () => {
  // §26's 安全阀管理. Widening has worked since M2 and nothing in the console
  // said so, so a Run stopped by its budget drew `cancel` and a dead end.
  let sent: unknown = null;
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(
        run({
          status: "paused",
          pause_reason: "limit",
          available_actions: ["widen_budget", "cancel"],
        }),
      ),
    ),
    http.post(`/api/v1/runs/${RUN}/budget`, async ({ request }) => {
      sent = await request.json();
      return HttpResponse.json(run({ status: "paused", available_actions: ["resume", "cancel"] }));
    }),
  );
  stream();

  renderRun();
  await userEvent.click(await screen.findByRole("button", { name: t("widenBudget") }));
  const field = await screen.findByLabelText(t("widenBudgetField"));
  await userEvent.clear(field);
  await userEvent.type(field, "30");
  await userEvent.click(screen.getByRole("button", { name: t("confirm") }));

  await waitFor(() =>
    // `expected_state_version` and not a bare number: this is a control like
    // any other, and a stale version has to be refused rather than applied to
    // whatever the Run became while the dialog was open.
    expect(sent).toEqual({ expected_state_version: 4, max_model_calls: 30 }),
  );
});

test("the dialog will not send a ceiling that is not a rise", async () => {
  // The API refuses it and writes `run.budget_widen_denied`. Letting the
  // console send it anyway spends an audit row on a typo, and answers the
  // administrator with a refusal instead of the field they mistyped.
  server.use(
    http.get(`/api/v1/runs/${RUN}`, () =>
      HttpResponse.json(
        run({ status: "paused", pause_reason: "limit", available_actions: ["widen_budget"] }),
      ),
    ),
    http.post(`/api/v1/runs/${RUN}/budget`, () => {
      throw new Error("the console should not have sent this");
    }),
  );
  stream();

  renderRun();
  await userEvent.click(await screen.findByRole("button", { name: t("widenBudget") }));
  const field = await screen.findByLabelText(t("widenBudgetField"));
  await userEvent.clear(field);
  await userEvent.type(field, "3");
  await userEvent.click(screen.getByRole("button", { name: t("confirm") }));

  expect(await screen.findByText(t("widenBudgetMustRise"))).toBeVisible();
});
