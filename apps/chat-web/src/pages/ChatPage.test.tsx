import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { ChatPage } from "./ChatPage";
import { rememberSessionId } from "../chat/localSessions";
import { AuthProvider } from "../auth/AuthProvider";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";
import { ChatTheme } from "../theme/ChatTheme";

const ALIAS = "darwin";
const SESSION = "33333333-4444-4555-8666-777777777777";
const RUN = "55555555-6666-4777-8888-999999999999";
const APPROVAL = "77777777-8888-4999-a000-111111111111";

const IDENTITY = { end_user_id: "draft-user-a", workspace_id: "draft-workspace" };
beforeEach(() => {
  window.sessionStorage.clear();
  server.use(http.get("/api/v1/end-user/me", () => HttpResponse.json(IDENTITY)));
});

test("a refreshed conversation restores its draft only after confirming the same identity", async () => {
  rememberSessionId(ALIAS, SESSION);
  server.use(http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])));
  const first = renderChat(`/${ALIAS}/${SESSION}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "只给当前用户恢复");
  first.unmount();
  const second = renderChat(`/${ALIAS}/${SESSION}`);
  expect(await screen.findByLabelText("写给智能体")).toHaveValue("只给当前用户恢复");
  second.unmount();
  server.use(http.get("/api/v1/end-user/me", () => HttpResponse.json({ ...IDENTITY, end_user_id: "draft-user-b" })));
  const other = renderChat(`/${ALIAS}/${SESSION}`);
  expect(await screen.findByLabelText("写给智能体")).toHaveValue("");
  other.unmount();
  server.use(http.get("/api/v1/end-user/me", () => HttpResponse.json({ detail: "Session expired" }, { status: 401 })));
  renderChat(`/${ALIAS}/${SESSION}`);
  expect(await screen.findByText("Session expired")).toBeInTheDocument();
  expect(screen.queryByLabelText("写给智能体")).toBeNull();
});

test("a lost first-send response survives refresh and retries once with the original key", async () => {
  const keys: (string | null)[] = [];
  server.use(
    http.post(`/api/v1/end-user/agents/${ALIAS}/sessions`, () => HttpResponse.json(sessionRow(), { status: 201 })),
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, ({ request }) => {
      keys.push(request.headers.get("Idempotency-Key"));
      return keys.length === 1 ? HttpResponse.error() : HttpResponse.json(finishedRun(), { status: 201 });
    }),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(finishedRun())),
  );
  const first = renderChat(`/${ALIAS}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "首次发送后响应丢失");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(keys).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
  first.unmount();
  const restored = renderChat(`/${ALIAS}/${SESSION}`);
  expect(await screen.findByLabelText("写给智能体")).toHaveValue("首次发送后响应丢失");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(keys).toHaveLength(2));
  expect(keys[1]).toBe(keys[0]);
  await waitFor(() => expect(screen.getByLabelText("写给智能体")).toHaveValue(""));
  restored.unmount();
  const afterSuccess = renderChat(`/${ALIAS}/${SESSION}`);
  expect(await screen.findByLabelText("写给智能体")).toHaveValue("");
  afterSuccess.unmount();
  renderChat(`/${ALIAS}`);
  expect(await screen.findByLabelText("写给智能体")).toHaveValue("");
});

test("cached identity cannot reveal a draft while a returning page checks the current cookie", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const first = renderChat(`/${ALIAS}`, client);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "不要显示旧身份的草稿");
  first.unmount();
  let release!: () => void;
  let checking = false;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  server.use(http.get("/api/v1/end-user/me", async () => {
    checking = true;
    await pending;
    return HttpResponse.json({ ...IDENTITY, end_user_id: "new-cookie-owner" });
  }));
  renderChat(`/${ALIAS}`, client);
  await waitFor(() => expect(checking).toBe(true));
  try { expect(screen.queryByLabelText("写给智能体")).toBeNull(); }
  finally { release(); }
  expect(await screen.findByLabelText("写给智能体")).toHaveValue("");
});

test("clearing a failed message makes an identical later message a new request", async () => {
  rememberSessionId(ALIAS, SESSION);
  const keys: (string | null)[] = [];
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, ({ request }) => {
      keys.push(request.headers.get("Idempotency-Key"));
      return HttpResponse.error();
    }),
  );
  renderChat(`/${ALIAS}/${SESSION}`);
  const input = await screen.findByLabelText("写给智能体");
  await userEvent.type(input, "重新提出同样的问题");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(input).toBeEnabled());
  await userEvent.clear(input);
  await userEvent.type(input, "重新提出同样的问题");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(keys).toHaveLength(2));
  expect(keys[1]).not.toBe(keys[0]);
});

for (const limit of [0, 50]) {
  test(`storage limited to ${limit} characters preserves first-send input and refuses unsafe submission`, async () => {
    let created = 0;
    let runs = 0;
    server.use(
      http.post(`/api/v1/end-user/agents/${ALIAS}/sessions`, () => {
        created += 1; return HttpResponse.json(sessionRow(), { status: 201 });
      }),
      http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
      http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, () => {
        runs += 1; return HttpResponse.json(finishedRun(), { status: 201 });
      }),
      http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(finishedRun())),
    );
    renderChat(`/${ALIAS}`);
    const input = await screen.findByLabelText("写给智能体");
    const original = Storage.prototype.setItem;
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (value.length > limit) throw new DOMException("Quota exceeded", "QuotaExceededError");
      original.call(this, key, value);
    });
    try {
      await userEvent.type(input, "保留我的输入");
      await userEvent.click(screen.getByRole("button", { name: "发送" }));
      expect(await screen.findByText("无法保存发送状态，本次未发送。请复制输入，重新打开页面后重试。")).toBeInTheDocument();
      expect(screen.getByLabelText("写给智能体")).toHaveValue("保留我的输入");
      expect(created).toBe(0);
      expect(runs).toBe(0);
    } finally { storage.mockRestore(); }
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(runs).toBe(1));
  });
}

test("reopening a conversation exposes its saved files without an active run", async () => {
  rememberSessionId(ALIAS, SESSION);
  server.use(
    http.get("/api/v1/end-user/agents", () => HttpResponse.json([])),
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.get(`/api/v1/end-user/sessions/${SESSION}/files`, () => HttpResponse.json({
      revision_id: "revision-1", items: [{ path: "notes/summary.md", size_bytes: 42 }],
    })),
    http.get(`/api/v1/end-user/sessions/${SESSION}/files/content`, ({ request }) => {
      expect(new URL(request.url).searchParams.get("revision_id")).toBe("revision-1");
      return HttpResponse.json({ code: "workspace_files_changed" }, { status: 409 });
    }),
  );
  renderChat(`/${ALIAS}/${SESSION}`);
  await userEvent.click(await screen.findByRole("button", { name: "下载 notes/summary.md" }));
  expect(await screen.findByText("文件已更新，请从刷新后的列表重新下载。")).toBeInTheDocument();
});

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

function renderChat(path: string, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })): ReturnType<typeof render> {
  return render(
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
  expect(screen.getByText("有什么需要帮忙的？")).toBeInTheDocument();
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

test("a failed send keeps the draft and can be retried after the service recovers", async () => {
  rememberSessionId(ALIAS, SESSION);
  let recovered = false;
  const submitted: unknown[] = [];
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, async ({ request }) => {
      if (!recovered) return HttpResponse.json({ detail: "Temporarily unavailable" }, { status: 503 });
      submitted.push(await request.json());
      return HttpResponse.json(finishedRun(), { status: 201 });
    }),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(finishedRun())),
  );
  renderChat(`/${ALIAS}/${SESSION}`);
  const input = await screen.findByLabelText("写给智能体");
  await userEvent.type(input, "请保留这段输入");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(await screen.findByText("Temporarily unavailable")).toBeInTheDocument();
  expect(input).toHaveValue("请保留这段输入");
  recovered = true;
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(submitted).toEqual([{ input: "请保留这段输入" }]));
  await waitFor(() => expect(input).toHaveValue(""));
});

test("retrying a lost send response reuses the request key to avoid duplicate work", async () => {
  rememberSessionId(ALIAS, SESSION);
  const keys: (string | null)[] = [];
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, ({ request }) => {
      keys.push(request.headers.get("Idempotency-Key"));
      return keys.length === 1 ? HttpResponse.error() : HttpResponse.json(finishedRun(), { status: 201 });
    }),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(finishedRun())),
  );
  renderChat(`/${ALIAS}/${SESSION}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "只执行一次");
  const send = screen.getByRole("button", { name: "发送" });
  await userEvent.click(send);
  await waitFor(() => expect(send).toBeEnabled());
  await userEvent.click(send);
  await waitFor(() => expect(keys).toHaveLength(2));
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
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

test("plan §10: a running run offers stop, and confirming it cancels the run", async () => {
  const cancellations: { runId: string; body: unknown }[] = [];
  const running = finishedRun({ status: "running", finished_at: null, state_version: 1 });
  const cancelled = finishedRun({
    status: "cancelled",
    finished_at: "2026-08-10T02:00:02Z",
    state_version: 2,
    queue: { position: 0, status: "terminal" },
  });
  server.use(
    http.post(`/api/v1/end-user/agents/${ALIAS}/sessions`, () =>
      HttpResponse.json(sessionRow(), { status: 201 }),
    ),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, () =>
      HttpResponse.json(running, { status: 201 }),
    ),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(running)),
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.post(`/api/v1/end-user/runs/${RUN}/cancel`, async ({ request }) => {
      cancellations.push({ runId: RUN, body: await request.json() });
      return HttpResponse.json(cancelled);
    }),
  );

  renderChat(`/${ALIAS}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "Keep working");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  await userEvent.click(await screen.findByRole("button", { name: "停止" }));
  expect(screen.getByText("确定要取消这次运行吗？取消后无法恢复。")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "取消运行" }));

  await waitFor(() => expect(cancellations).toHaveLength(1));
  expect(cancellations[0]?.body).toEqual({ expected_state_version: 1 });
  await waitFor(() =>
    expect(screen.queryByText("确定要取消这次运行吗？取消后无法恢复。")).toBeNull(),
  );
});

test("plan §10: a run waiting on the end user's own confirmation shows it, and approving it clears the banner", async () => {
  const decisions: { id: string; body: unknown }[] = [];
  let decided = false;
  const waiting = finishedRun({
    status: "waiting_approval",
    finished_at: null,
    queue: { position: 1, status: "waiting" },
  });
  const pendingApproval = {
    id: APPROVAL,
    run_id: RUN,
    approval_type: "user_confirmation",
    status: "pending",
    tool: "http.orders.createOrder",
    document: { tool: "http.orders.createOrder" },
    required_permission: null,
    requested_by: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    expires_at: "2026-08-10T03:00:00Z",
  };
  server.use(
    http.post(`/api/v1/end-user/agents/${ALIAS}/sessions`, () =>
      HttpResponse.json(sessionRow(), { status: 201 }),
    ),
    http.post(`/api/v1/end-user/sessions/${SESSION}/runs`, () =>
      HttpResponse.json(waiting, { status: 201 }),
    ),
    http.get(`/api/v1/end-user/runs/${RUN}`, () => HttpResponse.json(waiting)),
    http.get(`/api/v1/end-user/sessions/${SESSION}/messages`, () => HttpResponse.json([])),
    http.get("/api/v1/end-user/approvals", () =>
      HttpResponse.json(decided ? [] : [pendingApproval]),
    ),
    http.post(`/api/v1/end-user/approvals/${APPROVAL}/decision`, async ({ request }) => {
      decided = true;
      decisions.push({ id: APPROVAL, body: await request.json() });
      return HttpResponse.json({ ...pendingApproval, status: "approved" });
    }),
  );

  renderChat(`/${ALIAS}`);
  await userEvent.type(await screen.findByLabelText("写给智能体"), "Place an order");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  expect(await screen.findByText("这次运行需要你确认才能继续")).toBeInTheDocument();
  expect(screen.getByText("http.orders.createOrder")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "同意" }));

  await waitFor(() => expect(decisions).toHaveLength(1));
  expect(decisions[0]?.body).toEqual({ decision: "approve", reason: null });
  await waitFor(() => expect(screen.queryByText("这次运行需要你确认才能继续")).toBeNull());
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
  await userEvent.click(screen.getByRole("button", { name: "从列表移除" }));
  await userEvent.click(screen.getByRole("button", { name: "确认移除" }));

  expect(screen.queryByRole("button", { name: "Summarize yesterday" })).toBeNull();
});
