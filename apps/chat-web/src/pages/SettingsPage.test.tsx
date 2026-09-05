import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { SettingsPage } from "./SettingsPage";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";
import { ChatTheme } from "../theme/ChatTheme";

const AGENTS = [
  { alias: "support-bot", name: "Support Bot" },
  { alias: "weekly-report", name: "周报助手" },
];

function renderSettings(agents = AGENTS): void {
  // The page now asks which Agents the credential allows, for the default-Agent
  // choice. Listed first so a test's own handlers still win.
  server.use(http.get("/api/v1/end-user/agents", () => HttpResponse.json(agents)));
  render(
    <ChatTheme>
      <LocaleProvider>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter initialEntries={["/settings"]}>
            <SettingsPage />
          </MemoryRouter>
        </QueryClientProvider>
      </LocaleProvider>
    </ChatTheme>,
  );
}

test("the page has no account section — the platform was never given a name or email", () => {
  renderSettings();

  expect(screen.queryByText("账号")).toBeNull();
  expect(screen.queryByText("名称")).toBeNull();
  expect(screen.queryByText("默认智能体")).toBeNull();
});

test("export calls the self-service door and offers the response as a file", async () => {
  let requested = 0;
  server.use(
    http.get("/api/v1/end-user/subjects/me/export", () => {
      requested += 1;
      return HttpResponse.json({
        subject_type: "end_user",
        subject_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        workspace_id: "11111111-2222-4333-8444-555555555555",
        memories: [],
        sessions: [],
      });
    }),
  );
  const clicked: string[] = [];
  const originalClick = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) {
    clicked.push(this.download);
  };
  try {
    renderSettings();
    await userEvent.click(screen.getByRole("button", { name: "导出" }));
    // At least once, not exactly once: the page also reads this door on open,
    // to *show* what is held (§4.6's 查看). What this test is about is that
    // the download comes from the self-service door and carries its answer.
    await waitFor(() => expect(requested).toBeGreaterThanOrEqual(1));
    await waitFor(() => expect(clicked).toEqual(["tiny-hermes-my-data.json"]));
  } finally {
    HTMLAnchorElement.prototype.click = originalClick;
  }
});

test("erase asks for confirmation before calling the self-service door", async () => {
  let requested = 0;
  server.use(
    http.post("/api/v1/end-user/subjects/me/erase", () => {
      requested += 1;
      return HttpResponse.json({ memories: 2, sessions: 1, messages: 5, artifacts: 0 });
    }),
  );
  renderSettings();

  await userEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(requested).toBe(0);
  expect(screen.getByText(/确定要删除吗/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "删除" }));

  await waitFor(() => expect(requested).toBe(1));
  expect(await screen.findByText("已删除。")).toBeInTheDocument();
});

const MEMORY = {
  id: "m1",
  workspace_id: "w1",
  agent_id: "a1",
  kind: "private",
  status: "active",
  body: "They prefer mornings.",
  origin: "agent_proposal",
  created_at: "2026-08-02T00:00:00Z",
  updated_at: "2026-08-02T00:00:00Z",
};

function held(memories: object[] = [MEMORY]) {
  return {
    subject_type: "end_user",
    subject_id: "s1",
    workspace_id: "w1",
    memories,
    sessions: [],
  };
}

test("what is remembered is shown here, not only offered as a download", async () => {
  // §4.6 gives an end user 查看 alongside 更正, 删除 and 导出. A JSON file is
  // not 查看 — it is the same data behind a step most people will not take,
  // and it cannot be the thing a correction button sits next to.
  server.use(http.get("/api/v1/end-user/subjects/me/export", () => HttpResponse.json(held())));

  renderSettings();

  expect(await screen.findByText("They prefer mornings.")).toBeVisible();
});

test("a memory can be corrected, and the correction is what is sent", async () => {
  let sent: unknown = null;
  server.use(
    http.get("/api/v1/end-user/subjects/me/export", () => HttpResponse.json(held())),
    http.post("/api/v1/end-user/subjects/memories/m1/correct", async ({ request }) => {
      sent = await request.json();
      return HttpResponse.json({ ...MEMORY, body: "They prefer afternoons." });
    }),
  );

  renderSettings();
  await userEvent.click(await screen.findByRole("button", { name: "更正" }));
  const field = await screen.findByLabelText("更正后的内容");
  await userEvent.clear(field);
  await userEvent.type(field, "They prefer afternoons.");
  await userEvent.click(screen.getByRole("button", { name: "保存" }));

  await waitFor(() => expect(sent).toEqual({ body: "They prefer afternoons." }));
});

test("forgetting one memory does not go through erase-everything", async () => {
  // The two are different requests with different consequences, and only
  // one of them signs the person out of every chat they have.
  let forgot: string | null = null;
  server.use(
    http.get("/api/v1/end-user/subjects/me/export", () => HttpResponse.json(held())),
    http.post("/api/v1/end-user/subjects/memories/m1/forget", ({ request }) => {
      forgot = new URL(request.url).pathname;
      return HttpResponse.json({ ...MEMORY, status: "forgotten" });
    }),
    http.post("/api/v1/end-user/subjects/me/erase", () => {
      throw new Error("forgetting one memory must not erase everything");
    }),
  );

  renderSettings();
  // "删除这条", not "删除" — the erase-everything button says the latter,
  // and two buttons whose labels differ by nothing are a hazard on the page
  // before they are a problem in a test.
  await userEvent.click(await screen.findByRole("button", { name: "删除这条" }));

  await waitFor(() =>
    expect(forgot).toBe("/api/v1/end-user/subjects/memories/m1/forget"),
  );
});

test("with nothing remembered, the page says so rather than showing an empty box", async () => {
  server.use(
    http.get("/api/v1/end-user/subjects/me/export", () => HttpResponse.json(held([]))),
  );

  renderSettings();

  expect(await screen.findByText("目前没有关于你的记录")).toBeVisible();
});

test("默认 Agent 只在凭据允许的范围里选，只记在这台设备上", async () => {
  // 不是账号设置：平台没有这个人的名字，这条偏好也不上传，它是这台设备记住
  // 「下次先进哪个 Agent」，而且选项只有凭据点名的那几个。
  renderSettings();

  await userEvent.click(await screen.findByRole("button", { name: /周报助手/ }));

  expect(window.localStorage.getItem("tiny-hermes-chat-default-agent")).toBe("weekly-report");
  expect(screen.getByRole("button", { name: /周报助手/ })).toHaveAttribute("aria-pressed", "true");
});

test("凭据只允许一个 Agent 时没有可选的，页面直说", async () => {
  renderSettings([AGENTS[0]!]);
  expect(await screen.findByText("凭据只允许一个 Agent，没有可选的。")).toBeVisible();
});
