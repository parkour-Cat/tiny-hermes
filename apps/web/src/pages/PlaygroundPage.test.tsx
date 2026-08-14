import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { PlaygroundPage } from "./PlaygroundPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";
const SESSION = "33333333-4444-4555-8666-777777777777";
const HEAD = "44444444-5555-4666-8777-888888888888";
const PENDING = "55555555-6666-4777-8888-999999999999";
const USER = {
  id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  subject: "dev@example.com",
  display_name: "Dev",
  status: "active",
  is_platform_admin: false,
};

const BUDGET = {
  max_execution_seconds: 600,
  consumed_execution_ms: 0,
  max_elapsed_seconds: 3_600,
  elapsed_deadline_at: "2026-08-10T03:00:00Z",
  max_model_calls: 12,
  consumed_model_calls: 0,
  max_tool_calls: 7,
  consumed_tool_calls: 0,
  max_tokens: null,
  consumed_tokens: 0,
  max_derived_retries: 2,
  derived_retry_count: 0,
};

function sessionRow(overrides: Record<string, unknown> = {}) {
  return {
    id: SESSION,
    agent_id: AGENT,
    session_mode: "persistent",
    caller_type: "user",
    caller_id: USER.id,
    head_run_id: null,
    next_run_sequence: 1,
    next_message_sequence: 1,
    created_at: "2026-08-10T02:00:00Z",
    ...overrides,
  };
}

function runRow(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    session_id: SESSION,
    agent_version_id: "v1",
    status: "running",
    state_version: 4,
    session_sequence: 1,
    blocked_by_run_id: null,
    pause_reason: null,
    wait_kind: null,
    wait_deadline_at: null,
    retry_of_run_id: null,
    budget_root_run_id: id,
    last_event_sequence: 0,
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

function held() {
  const body = new ReadableStream({
    start() {
      // Left open: a live Run's stream does not end.
    },
  });
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
}

function loadedPlayground(): void {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get(`/api/v1/agents/${AGENT}`, () =>
      HttpResponse.json({
        id: AGENT,
        name: "Analyst",
        alias: "analyst",
        status: "published",
        current_version_id: "v1",
        created_at: "2026-08-10T00:00:00Z",
      }),
    ),
    http.get("/api/v1/sessions", () => HttpResponse.json([sessionRow()])),
    http.get(`/api/v1/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
  );
}

function renderPlayground(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/agents/${AGENT}/playground`]}>
          <AuthProvider>
            <Routes>
              <Route
                path="/workspaces/:workspaceId/agents/:agentId/playground"
                element={<PlaygroundPage />}
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("sending a message posts a run with a fresh idempotency key", async () => {
  loadedPlayground();
  const sent: { key: string | null; body: unknown }[] = [];
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.post("/api/v1/runs", async ({ request }) => {
      const created = runRow(PENDING);
      sent.push({ key: request.headers.get("Idempotency-Key"), body: await request.json() });
      return HttpResponse.json(created, { status: 201 });
    }),
    http.get(`/api/v1/runs/${PENDING}`, () => HttpResponse.json(runRow(PENDING))),
    http.get(`/api/v1/runs/${PENDING}/events`, () => held()),
    http.get(`/api/v1/runs/${PENDING}/artifacts`, () => HttpResponse.json([])),
  );

  renderPlayground();
  await userEvent.type(await screen.findByLabelText("输入要发给智能体的消息"), "Hello");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]?.key).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  expect(sent[0]?.body).toEqual({ session_id: SESSION, input: "Hello" });
});

test("a blocked queue offers the head run's actions, not a fake completions refusal", async () => {
  loadedPlayground();
  document.cookie = "tiny_hermes_csrf=token-value";
  const resumes: { url: string; body: unknown }[] = [];
  const blocked = runRow(PENDING, {
    status: "queued",
    blocked_by_run_id: HEAD,
    available_actions: [],
    queue: {
      position: 2,
      status: "session_blocked",
      blocked_by_run_id: HEAD,
      head_status: "paused",
      head_reason: { pause_reason: "manual", wait_kind: null, wait_deadline_at: null },
      available_actions: ["resume"],
    },
  });
  server.use(
    http.post("/api/v1/runs", () => HttpResponse.json(blocked, { status: 201 })),
    http.get(`/api/v1/runs/${PENDING}`, () => HttpResponse.json(blocked)),
    http.get(`/api/v1/runs/${PENDING}/events`, () => held()),
    http.get(`/api/v1/runs/${PENDING}/artifacts`, () => HttpResponse.json([])),
    http.get(`/api/v1/runs/${HEAD}`, () =>
      HttpResponse.json(runRow(HEAD, { status: "paused", available_actions: ["resume"] })),
    ),
    http.post(`/api/v1/runs/${HEAD}/resume`, async ({ request }) => {
      resumes.push({ url: request.url, body: await request.json() });
      return HttpResponse.json(runRow(HEAD, { status: "running", state_version: 5 }));
    }),
  );

  renderPlayground();
  await userEvent.type(await screen.findByLabelText("输入要发给智能体的消息"), "Next");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  expect(await screen.findByText("当前会话被队列挡住")).toBeInTheDocument();
  expect(screen.getByText("已暂停")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "继续" }));

  await waitFor(() => expect(resumes).toHaveLength(1));
  expect(resumes[0]?.url).toContain(`/api/v1/runs/${HEAD}/resume`);
  expect(resumes[0]?.body).toEqual({ expected_state_version: 4 });
});

test("新会话 posts another persistent session and switches to it", async () => {
  loadedPlayground();
  document.cookie = "tiny_hermes_csrf=token-value";
  const created = sessionRow({
    id: "66666666-7777-4888-8999-aaaaaaaaaaaa",
    next_run_sequence: 1,
  });
  const posts: unknown[] = [];
  server.use(
    http.post("/api/v1/sessions", async ({ request }) => {
      posts.push(await request.json());
      return HttpResponse.json(created, { status: 201 });
    }),
    http.get(`/api/v1/sessions/${created.id}/messages`, () => HttpResponse.json([])),
  );

  renderPlayground();
  await screen.findByText(SESSION);
  await userEvent.click(screen.getByRole("button", { name: "新会话" }));

  await waitFor(() => expect(posts).toEqual([{ agent_id: AGENT, session_mode: "persistent" }]));
  expect(await screen.findByText(created.id)).toBeInTheDocument();
});
