import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { RunsPage } from "./RunsPage";
import { moment } from "../i18n/moment";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";
const SESSION = "33333333-4444-4555-8666-777777777777";
const HEAD_RUN = "44444444-5555-4666-8777-888888888888";
const QUEUED_RUN = "55555555-6666-4777-8888-999999999999";

const BUDGET = {
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
};

function runRow(overrides: Record<string, unknown>) {
  return {
    id: HEAD_RUN,
    session_id: SESSION,
    agent_version_id: "66666666-7777-4888-8999-aaaaaaaaaaaa",
    status: "running",
    state_version: 2,
    session_sequence: 1,
    blocked_by_run_id: null,
    pause_reason: null,
    wait_kind: null,
    wait_deadline_at: null,
    retry_of_run_id: null,
    budget_root_run_id: HEAD_RUN,
    last_event_sequence: 4,
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

const AGENTS = [
  {
    id: AGENT,
    name: "Analyst",
    alias: "analyst",
    status: "published",
    current_version_id: "66666666-7777-4888-8999-aaaaaaaaaaaa",
    created_at: "2026-08-10T00:00:00Z",
  },
];

function listing(runs: unknown[]): void {
  server.use(
    http.get("/api/v1/runs", () => HttpResponse.json(runs)),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );
}

function renderRuns(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/runs`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/runs" element={<RunsPage />} />
            <Route path="/workspaces/:workspaceId/runs/:runId" element={<p>run detail</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

/** The row a Run occupies, found by the address that names it. */
async function rowOf(runId: string): Promise<HTMLElement> {
  const link = await screen.findByRole("link", {
    name: (_accessible, element) => element.getAttribute("href")?.endsWith(`/runs/${runId}`) === true,
  });
  const row = link.closest("tr");
  if (row === null) {
    throw new Error(`no row for ${runId}`);
  }
  return row;
}

test("a row states the Run's status, its place in the session, and its times", async () => {
  listing([
    runRow({
      status: "completed",
      finished_at: "2026-08-10T02:04:00Z",
      queue: { position: 0, status: "terminal" },
    }),
  ]);

  renderRuns();
  const row = await rowOf(HEAD_RUN);

  expect(within(row).getByText("已完成")).toBeInTheDocument();
  expect(within(row).getByText("1")).toBeInTheDocument();
  expect(within(row).getByText(moment("2026-08-10T02:00:00Z"))).toBeInTheDocument();
  expect(within(row).getByText(moment("2026-08-10T02:04:00Z"))).toBeInTheDocument();
});

test("a Run behind another one shows where it is queued, and the head Run does not", async () => {
  listing([
    runRow({}),
    runRow({
      id: QUEUED_RUN,
      status: "queued",
      session_sequence: 2,
      started_at: null,
      queue: { position: 3, status: "session_blocked" },
    }),
  ]);

  renderRuns();

  // Phase 2A accepts a Run submitted into a busy Session rather than refusing
  // it; the position is the half of that promise the user can see.
  expect(within(await rowOf(QUEUED_RUN)).getByText("排队第 3 位")).toBeInTheDocument();
  expect(within(await rowOf(HEAD_RUN)).queryByText(/排队第/)).not.toBeInTheDocument();
});

test("a running Run has no end time, and the column says so", async () => {
  listing([runRow({})]);

  renderRuns();

  expect(within(await rowOf(HEAD_RUN)).getByText("—")).toBeInTheDocument();
});

test("the page neither pages the list nor pretends the platform can", async () => {
  listing([runRow({})]);

  renderRuns();
  await rowOf(HEAD_RUN);

  expect(
    screen.queryByText("接口一次返回全部任务记录，没有分页，也没有筛选。记录很多时列表会变慢。"),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Analyst · 第 1 次" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/runs/${HEAD_RUN}`,
  );
  expect(screen.queryByRole("link", { name: HEAD_RUN })).not.toBeInTheDocument();
  // A pager over a list that arrived whole would be a control that pretends to
  // ask the platform for something.
  expect(screen.queryByRole("listitem", { name: /page/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("list", { name: /pagination/i })).not.toBeInTheDocument();
});

/** Fills in the submission dialog and presses 提交. */
async function submitRun(message: string): Promise<void> {
  await userEvent.click(await screen.findByRole("button", { name: "提交任务" }));
  await userEvent.click(await screen.findByLabelText("智能体"));
  await userEvent.click(await screen.findByTitle("Analyst"));
  await userEvent.type(await screen.findByLabelText("输入"), message);
  await userEvent.click(screen.getByRole("button", { name: "提交" }));
}

test("submitting opens a session, posts the Run under an idempotency key, and goes to it", async () => {
  listing([]);
  const sessions: unknown[] = [];
  const posted: { body: unknown; key: string | null }[] = [];
  server.use(
    http.post("/api/v1/sessions", async ({ request }) => {
      sessions.push(await request.json());
      return HttpResponse.json(
        {
          id: SESSION,
          agent_id: AGENT,
          session_mode: "persistent",
          caller_type: "user",
          caller_id: "77777777-8888-4999-8aaa-bbbbbbbbbbbb",
          head_run_id: null,
          next_run_sequence: 1,
          next_message_sequence: 1,
          created_at: "2026-08-10T02:00:00Z",
        },
        { status: 201 },
      );
    }),
    http.post("/api/v1/runs", async ({ request }) => {
      posted.push({ body: await request.json(), key: request.headers.get("Idempotency-Key") });
      return HttpResponse.json(runRow({}), { status: 201 });
    }),
  );

  renderRuns();
  await submitRun("Summarize the incident.");

  expect(await screen.findByText("run detail")).toBeInTheDocument();
  expect(sessions).toEqual([{ agent_id: AGENT, session_mode: "persistent" }]);
  expect(posted).toHaveLength(1);
  expect(posted[0]?.body).toEqual({ session_id: SESSION, input: "Summarize the incident." });
  // The server refuses an empty key, and a key that is not a key makes the
  // idempotency record decorative.
  expect(posted[0]?.key).toMatch(/\S/);
});

test("an unpublished agent is answered with what has to happen first", async () => {
  listing([]);
  server.use(
    http.post("/api/v1/sessions", () =>
      HttpResponse.json(
        { code: "agent_not_published", detail: "The agent has no published version to run." },
        { status: 409 },
      ),
    ),
  );

  renderRuns();
  await submitRun("Summarize the incident.");

  expect(await screen.findByText("该智能体尚未发布，请先发布后再提交任务")).toBeInTheDocument();
  expect(screen.queryByText("run detail")).not.toBeInTheDocument();
});

test("a reused idempotency key is reported, and nothing is sent again on its own", async () => {
  listing([]);
  let attempts = 0;
  server.use(
    http.post("/api/v1/sessions", () =>
      HttpResponse.json(
        {
          id: SESSION,
          agent_id: AGENT,
          session_mode: "persistent",
          caller_type: "user",
          caller_id: "77777777-8888-4999-8aaa-bbbbbbbbbbbb",
          head_run_id: null,
          next_run_sequence: 1,
          next_message_sequence: 1,
          created_at: "2026-08-10T02:00:00Z",
        },
        { status: 201 },
      ),
    ),
    http.post("/api/v1/runs", () => {
      attempts += 1;
      return HttpResponse.json(
        {
          code: "idempotency_key_reused",
          detail: "That idempotency key already belongs to a different request.",
        },
        { status: 409 },
      );
    }),
  );

  renderRuns();
  await submitRun("Summarize the incident.");

  expect(
    await screen.findByText("该提交标识已属于另一次请求，请重新提交"),
  ).toBeInTheDocument();
  // Resending is how a console turns the idempotency record into a formality.
  await waitFor(() => expect(attempts).toBe(1));
});
