import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ChatPage } from "./ChatPage";
import { rememberSessionId } from "../chat/localSessions";
import { AuthProvider } from "../auth/AuthProvider";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";
import { ChatTheme } from "../theme/ChatTheme";

const ALIAS = "darwin";
const SESSION = "33333333-4444-4555-8666-777777777777";
const RUN = "55555555-6666-4777-8888-999999999999";

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
    agent_id: "22222222-3333-4444-8555-666666666666",
    session_mode: "persistent",
    caller_type: "end_user",
    caller_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    head_run_id: null,
    next_run_sequence: 1,
    next_message_sequence: 1,
    created_at: "2026-08-10T02:00:00Z",
    ...overrides,
  };
}

/** A run that is already finished, so `useEndUserRun`'s polling stops on
 * its first read — every test here stands in for a scenario the platform's
 * deterministic model completes synchronously. */
function finishedRun(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN,
    session_id: SESSION,
    agent_version_id: "v1",
    status: "completed",
    state_version: 2,
    session_sequence: 1,
    blocked_by_run_id: null,
    pause_reason: null,
    wait_kind: null,
    wait_deadline_at: null,
    retry_of_run_id: null,
    budget_root_run_id: RUN,
    last_event_sequence: 1,
    queue: { position: 1, status: "terminal" },
    budget: BUDGET,
    available_actions: [],
    checkpoint_replay_safe: true,
    checkpoint_effect_status: "none",
    created_at: "2026-08-10T02:00:00Z",
    started_at: "2026-08-10T02:00:00Z",
    finished_at: "2026-08-10T02:00:01Z",
    ...overrides,
  };
}

function renderChat(path: string): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ChatTheme>
      <LocaleProvider>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={[path]}>
            <AuthProvider>
              <Routes>
                <Route path="/:alias/:sessionRef" element={<ChatPage />} />
                <Route path="/:alias" element={<ChatPage />} />
              </Routes>
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </LocaleProvider>
    </ChatTheme>,
  );
}

test("an empty conversation offers the composer and no console chrome", async () => {
  renderChat(`/${ALIAS}`);

  expect(await screen.findByLabelText("写给智能体")).toBeInTheDocument();
  expect(screen.getByText(/直接说要做什么/)).toBeInTheDocument();
  expect(screen.queryByText("成员")).toBeNull();
  expect(screen.queryByText("API 密钥")).toBeNull();
  expect(document.querySelector("select")).toBeNull();
});

test("sending the first message creates a session and the reply appears", async () => {
  const created: { agent: string; body: unknown }[] = [];
  const submitted: { key: string | null; body: unknown }[] = [];
  server.use(
    http.post(`/api/v1/end-user/agents/${ALIAS}/sessions`, async ({ request }) => {
      created.push({ agent: ALIAS, body: await request.json() });
      return HttpResponse.json(sessionRow(), { status: 201 });
    }),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, async ({ request }) => {
      submitted.push({
        key: request.headers.get("Idempotency-Key"),
        body: await request.json(),
      });
      return HttpResponse.json(finishedRun(), { status: 201 });
    }),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(finishedRun())),
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () =>
      HttpResponse.json([
        { role: "user", parts: [{ type: "text", text: "Hello" }] },
        { role: "assistant", parts: [{ type: "text", text: "Hi there." }] },
      ]),
    ),
  );

  renderChat(`/${ALIAS}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "Hello");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() => expect(created).toHaveLength(1));
  expect(created[0]?.body).toEqual({});
  await waitFor(() => expect(submitted).toHaveLength(1));
  expect(submitted[0]?.key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  expect(submitted[0]?.body).toEqual({ input: "Hello" });
  expect(await screen.findByText("Hi there.")).toBeInTheDocument();
});

test("reopening the address for a known session shows the same conversation", async () => {
  rememberSessionId(ALIAS, SESSION);
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () =>
      HttpResponse.json([
        { role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] },
        { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
      ]),
    ),
  );

  renderChat(`/${ALIAS}/${SESSION.slice(0, 8)}`);

  expect(await screen.findByText("Here is the summary.")).toBeInTheDocument();
});

test("a blocked queue shows the wait, not a silent refusal", async () => {
  const blocked = finishedRun({
    status: "queued",
    finished_at: null,
    queue: {
      position: 2,
      status: "session_blocked",
      blocked_by_run_id: "44444444-5555-4666-8777-888888888888",
      head_status: "paused",
    },
  });
  server.use(
    http.post(`/api/v1/end-user/agents/${ALIAS}/sessions`, () =>
      HttpResponse.json(sessionRow(), { status: 201 }),
    ),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, () =>
      HttpResponse.json(blocked, { status: 201 }),
    ),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(blocked)),
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
  );

  renderChat(`/${ALIAS}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "Next");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  expect(await screen.findByText(/上一条任务还没结束/)).toBeInTheDocument();
  expect(screen.getByText(/也可以开一个新对话/)).toBeInTheDocument();
});

test("session-rail actions stay behind the row menu, backed by this device's memory", async () => {
  rememberSessionId(ALIAS, SESSION);
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () =>
      HttpResponse.json([{ role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] }]),
    ),
  );

  renderChat(`/${ALIAS}`);
  expect(await screen.findByRole("button", { name: "Summarize yesterday" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "会话操作" }));
  expect(screen.getByRole("dialog", { name: "会话操作" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "删除" }));
  await userEvent.click(screen.getByRole("button", { name: "确认删除" }));

  expect(screen.queryByRole("button", { name: "Summarize yesterday" })).toBeNull();
});
