import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, vi } from "vitest";

import { ChatPage } from "./ChatPage";
import { SettingsPage } from "./SettingsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";
import { ChatTheme } from "../theme/ChatTheme";

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
      // A live Run's stream does not end.
    },
  });
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
}

function publishedAgent(overrides: Record<string, unknown> = {}) {
  return {
    id: AGENT,
    name: "Darwin",
    alias: "darwin",
    status: "published",
    current_version_id: "v1",
    created_at: "2026-08-10T00:00:00Z",
    ...overrides,
  };
}

function loadedChat(messages: unknown[] = [], extraSessions: unknown[] = []): void {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/workspaces", () =>
      HttpResponse.json([{ id: WORKSPACE, name: "Acme", status: "active" }]),
    ),
    http.get("/api/v1/agents", () =>
      HttpResponse.json([
        publishedAgent(),
        {
          id: "99999999-aaaa-4bbb-8ccc-dddddddddddd",
          name: "Draft",
          alias: "draft",
          status: "draft",
          current_version_id: null,
          created_at: "2026-08-10T00:00:00Z",
        },
      ]),
    ),
    http.get(`/api/v1/agents/${AGENT}`, () => HttpResponse.json(publishedAgent())),
    http.get("/api/v1/sessions", () => HttpResponse.json([sessionRow(), ...extraSessions])),
    http.get(`/api/v1/sessions/${SESSION}/messages`, () => HttpResponse.json(messages)),
  );
}

function renderChat(path = `/${WORKSPACE}/${AGENT}`): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ChatTheme>
      <LocaleProvider>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={[path]}>
            <AuthProvider>
              <Routes>
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/:workspaceId/:agentId/:sessionId" element={<ChatPage />} />
                <Route path="/:workspaceId/:agentId" element={<ChatPage />} />
              </Routes>
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </LocaleProvider>
    </ChatTheme>,
  );
}

test("the page is a conversation, not a playground or a console", async () => {
  loadedChat();
  renderChat();

  expect(await screen.findByRole("heading", { name: "Darwin" })).toBeInTheDocument();
  expect(screen.getByLabelText("写给智能体")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "新对话" })).toBeInTheDocument();
  expect(screen.getByText(/直接说要做什么/)).toBeInTheDocument();
  expect(screen.queryByText("试验场")).toBeNull();
  expect(screen.queryByText("成员")).toBeNull();
  expect(screen.queryByText("API 密钥")).toBeNull();
  expect(screen.queryByText("机密")).toBeNull();
  expect(screen.queryByText("消息")).toBeNull();
  expect(document.querySelector(".ant-card")).toBeNull();
  expect(document.querySelector("select")).toBeNull();
  expect(screen.queryByText(SESSION)).toBeNull();
});

test("unpublished agents are not offered in the picker", async () => {
  loadedChat();
  renderChat();

  await screen.findByRole("heading", { name: "Darwin" });
  const picker = screen.getByLabelText("智能体");
  expect(picker).toHaveTextContent("Darwin");
  expect(picker).not.toHaveTextContent("Draft");
  await userEvent.click(picker);
  expect(screen.getByRole("option", { name: "Darwin" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Draft" })).toBeNull();
});

test("account actions stay behind the user name", async () => {
  loadedChat();
  renderChat();
  await screen.findByRole("heading", { name: "Darwin" });
  expect(screen.queryByRole("button", { name: "深色" })).toBeNull();
  expect(screen.queryByRole("link", { name: "设置" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Dev" }));
  expect(screen.getByRole("dialog", { name: "Dev" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "外观" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "语言" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "设置" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "退出" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "深色" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(screen.getByRole("dialog", { name: "Dev" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("link", { name: "设置" }));
  expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "默认智能体" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "关于" })).toBeInTheDocument();
  const darwin = await screen.findByRole("button", { name: /Darwin/ });
  await userEvent.click(darwin);
  expect(darwin).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("group", { name: "外观" })).toBeNull();
  expect(screen.queryByRole("group", { name: "语言" })).toBeNull();
  expect(screen.queryByText("成员")).toBeNull();
  expect(document.querySelector("select")).toBeNull();
});

test("session actions stay behind the row menu", async () => {
  loadedChat([{ role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] }]);
  renderChat();
  expect(await screen.findByRole("button", { name: "Summarize yesterday" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "置顶" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "会话操作" }));
  expect(screen.getByRole("dialog", { name: "会话操作" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "置顶" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "归档" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "置顶" }));
  expect(screen.queryByRole("dialog", { name: "会话操作" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "会话操作" }));
  expect(screen.getByRole("button", { name: "取消置顶" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "归档" }));
  expect(screen.getByText("已归档")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "会话操作" }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  await userEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(confirm).toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Summarize yesterday" })).toBeNull();
  confirm.mockRestore();
});

test("a thread uses the first user line as the session title", async () => {
  loadedChat([
    { role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] },
    { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
    {
      role: "assistant",
      parts: [{ type: "tool_call", call_id: "c1", name: "file.read", arguments: { path: "a.md" } }],
    },
    {
      role: "tool",
      parts: [{ type: "tool_result", call_id: "c1", output: "artifact_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" }],
    },
  ]);
  server.use(
    http.get(`/api/v1/runs/${PENDING}/artifacts`, () => HttpResponse.json([])),
  );
  renderChat();

  expect(await screen.findByRole("button", { name: "Summarize yesterday" })).toBeInTheDocument();
  expect(screen.getByText("Here is the summary.")).toBeInTheDocument();
  expect(screen.getByText("file.read")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "工具" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "产物" })).toBeNull();
});

test("sending a message posts a run with a fresh idempotency key", async () => {
  loadedChat();
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

  renderChat();
  await userEvent.type(await screen.findByLabelText("写给智能体"), "Hello");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]?.key).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  expect(sent[0]?.body).toEqual({ session_id: SESSION, input: "Hello" });
  expect(screen.getByText("Hello")).toBeInTheDocument();
  expect(screen.getByText("正在回复")).toBeInTheDocument();
});

test("a blocked queue shows the wait in the thread, not a completions refusal", async () => {
  loadedChat();
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

  renderChat();
  await userEvent.type(await screen.findByLabelText("写给智能体"), "Next");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  expect(await screen.findByText(/上一条任务还没结束/)).toBeInTheDocument();
  expect(screen.getByText(/已暂停/)).toBeInTheDocument();
  expect(screen.getByText(/也可以开一个新对话/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "继续" }));

  await waitFor(() => expect(resumes).toHaveLength(1));
  expect(resumes[0]?.url).toContain(`/api/v1/runs/${HEAD}/resume`);
  expect(resumes[0]?.body).toEqual({ expected_state_version: 4 });
});

test("a finished turn does not put retry in the page chrome", async () => {
  loadedChat([
    { role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] },
    { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
  ]);
  server.use(
    http.get("/api/v1/sessions", () =>
      HttpResponse.json([sessionRow({ head_run_id: PENDING })]),
    ),
    http.get(`/api/v1/runs/${PENDING}`, () =>
      HttpResponse.json(
        runRow(PENDING, {
          status: "completed",
          finished_at: "2026-08-10T02:01:00Z",
          available_actions: ["retry"],
          queue: { position: 1, status: "terminal" },
        }),
      ),
    ),
    http.get(`/api/v1/runs/${PENDING}/events`, () => held()),
    http.get(`/api/v1/runs/${PENDING}/artifacts`, () => HttpResponse.json([])),
  );

  renderChat();
  expect(await screen.findByText("Here is the summary.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
});

test("新对话 posts another persistent session", async () => {
  loadedChat();
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

  renderChat();
  await screen.findByRole("heading", { name: "Darwin" });
  await userEvent.click(screen.getByRole("button", { name: "新对话" }));

  await waitFor(() => expect(posts).toEqual([{ agent_id: AGENT, session_mode: "persistent" }]));
});
