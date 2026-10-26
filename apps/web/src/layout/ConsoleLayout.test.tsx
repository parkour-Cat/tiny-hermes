import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { theme } from "antd";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, test } from "vitest";

import { ConsoleLayout } from "./ConsoleLayout";
import { ConsoleTheme, consoleDesignToken } from "./ConsoleTheme";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { t } from "../i18n/zh-CN";
import { mediaMatches } from "../test/setup";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const DARK_QUERY = "(prefers-color-scheme: dark)";

afterEach(() => {
  window.localStorage.clear();
});

const ADMIN = {
  id: "u1",
  subject: "admin@example.com",
  display_name: "Admin",
  status: "active",
  is_platform_admin: true,
};

/** Counts every workspace listing the shell asks for. */
function countingWorkspaces(hits: { count: number }) {
  return http.get("/api/v1/workspaces", () => {
    hits.count += 1;
    return HttpResponse.json([{ id: WORKSPACE, name: "Acme", status: "active" }]);
  });
}

/** Where the console went, for tests that follow a jump. */
function WhereAmI() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.hash}</p>;
}

function renderShell(
  path: string,
  hits = { count: 0 },
  handlers: ReturnType<typeof http.get>[] = [],
): { hits: { count: number } } {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    countingWorkspaces(hits),
    // The shell now counts the inbox. A test that cares passes its own queue
    // handlers, listed *before* the empty ones: within one `use`, the earlier
    // handler is the one that answers.
    ...handlers,
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    ...emptyQueues(),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    // Themed exactly as the app themes it: `autoInsertSpace` off is what keeps
    // 退出 from rendering as 退 出, and a test that skipped it would be asserting
    // against a shell nobody ships.
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId" element={<ConsoleLayout />}>
                <Route path="agents" element={<p>agent list</p>} />
                <Route path="*" element={<WhereAmI />} />
              </Route>
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
  return { hits };
}

test("both sections stay inside the workspace the address names", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);

  // 七个入口。只有一段的三个直接指向那一段；合并的四个指向合并页，原来的
  // 十五个地址作为跳转长期保留（见 App.routes.test.tsx）。
  expect(await screen.findByRole("link", { name: "Agent" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/agents`,
  );
  expect(screen.getByRole("link", { name: "任务" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/runs`,
  );
  expect(screen.getByRole("link", { name: /渠道/ })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/channels`,
  );
  for (const [label, group] of [
    ["待办", "inbox"],
    ["能力", "tooling"],
    ["记录", "records"],
    ["设置", "settings"],
  ] as const) {
    expect(screen.getByRole("link", { name: new RegExp(label) })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE}/${group}`,
    );
  }
  expect(await screen.findByText("Acme")).toBeInTheDocument();
  expect(screen.getByText("agent list")).toBeInTheDocument();
});

test("workspace switching is available in the sidebar", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  expect(await screen.findByRole("combobox", { name: "切换工作空间" })).toBeVisible();
});

test("an address that cannot name a workspace is refused without asking the platform", async () => {
  const { hits } = renderShell("/workspaces/not-a-uuid/agents");

  expect(await screen.findByText("工作空间地址无效")).toBeInTheDocument();
  expect(screen.queryByText("agent list")).not.toBeInTheDocument();
  // The refusal is local because the address cannot name anything. Sending it
  // anyway would teach the console to treat a 4xx as normal traffic.
  await waitFor(() => expect(hits.count).toBe(0));
});

test("the shell names the signed-in user and can sign them out", async () => {
  let signedOut = false;
  server.use(
    http.delete("/api/v1/auth/sessions/current", () => {
      signedOut = true;
      return new HttpResponse(null, { status: 204 });
    }),
  );
  renderShell(`/workspaces/${WORKSPACE}/agents`);

  expect(await screen.findByText("admin@example.com")).toBeInTheDocument();
  // 退出是低频操作，收在侧栏底部的账号菜单里，不再占一条顶栏。
  await userEvent.click(screen.getByRole("button", { name: "账号与偏好" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "退出" }));

  await waitFor(() => expect(signedOut).toBe(true));
});

/** Reports the background the theme actually resolved to. */
function ThemeProbe() {
  const { token } = theme.useToken();
  return <span data-testid="container">{token.colorBgContainer}</span>;
}

function renderThemed(children: ReactNode): void {
  render(<ConsoleTheme>{children}</ConsoleTheme>);
}

test("a dark system preference selects the dark algorithm", () => {
  mediaMatches.set(DARK_QUERY, true);

  renderThemed(<ThemeProbe />);

  // The console's own paper, not the algorithm's default: both themes name
  // their surfaces in `consoleDesignToken`, and the dark one is a darker
  // paper rather than an inverted white.
  expect(screen.getByTestId("container")).toHaveTextContent(consoleDesignToken(true).colorBgContainer);
});

test("a light system preference keeps the default algorithm", () => {
  renderThemed(<ThemeProbe />);

  expect(screen.getByTestId("container")).toHaveTextContent(consoleDesignToken(false).colorBgContainer);
});

test("a stored dark theme wins over a light system preference", () => {
  window.localStorage.setItem("tiny-hermes-theme", "dark");

  renderThemed(<ThemeProbe />);

  expect(screen.getByTestId("container")).toHaveTextContent(consoleDesignToken(true).colorBgContainer);
});

test("the locale switcher changes chrome into English", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);

  expect(await screen.findByRole("link", { name: "任务" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "账号与偏好" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "English" }));

  expect(await screen.findByRole("link", { name: "Runs" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Settings/ })).toBeInTheDocument();
});

/** The three queues the inbox badge adds up, all empty unless a test says
 *  otherwise. Declared last so a test's own handler for one of them wins. */
function emptyQueues() {
  return [
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [], has_more: false })),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
  ];
}

test("平台管理员看到七个工作入口和独立平台管理", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  const nav = await screen.findByRole("navigation");
  expect(within(nav).getAllByRole("link")).toHaveLength(9);
});

test("每个入口都带着它自己那句说明", async () => {
  // 这些句子早就写好了，只是原来要走进去之后才看得到——而需要它们的时刻
  // 是「决定走进哪里」之前。
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  const inbox = await screen.findByRole("link", { name: new RegExp(t("navInbox")) });
  expect(inbox).toHaveAttribute("title", t("navInboxIntro"));
});

test("只有一段的入口直接指向那一段，合并的入口指向合并页", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  const nav = await screen.findByRole("navigation");
  expect(within(nav).getByRole("link", { name: new RegExp(t("channels")) })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/channels`,
  );
  expect(within(nav).getByRole("link", { name: new RegExp(t("navSettings")) })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/settings`,
  );
});

test("待办上的数字是三个队列的总数", async () => {
  // The approvals stub refuses without the workspace header, as the real
  // route does: the first walk found the badge missing for exactly that.
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, [
    http.get("/api/v1/approvals", ({ request }) =>
      request.headers.get("X-Workspace-Id") === WORKSPACE
        ? HttpResponse.json({ items: [{ id: "a" }, { id: "b" }], has_more: false })
        : HttpResponse.json({ code: "workspace_required" }, { status: 400 }),
    ),
    // Only pending proposals count; without the filter the route also lists
    // the decided ones (the second thing the first real walk found).
    http.get("/api/v1/skill-proposals", ({ request }) =>
      new URL(request.url).searchParams.get("status") === "pending"
        ? HttpResponse.json([{ id: "c" }])
        : HttpResponse.json([{ id: "c" }, { id: "decided" }]),
    ),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
  ]);
  expect(await screen.findByText("3")).toBeVisible();
});

test("有一个可处理队列读不到时显示未知", async () => {
  // 显示「2」而其实是「2 + 读不到」，比不显示更糟：它看起来是个准确的数。
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, [
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [{ id: "a" }, { id: "b" }], has_more: false })),
    http.get("/api/v1/skill-proposals", () => new HttpResponse(null, { status: 403 })),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
  ]);
  await screen.findByRole("navigation");
  expect(await screen.findByText("?")).toBeVisible();
  expect(screen.queryByText("2")).toBeNull();
});

test("导航分成首页、日常、搭建、管理四组", async () => {
  // 九个平铺入口找不到东西：按「每天用的」和「搭一次就不常动的」分开。
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  const nav = await screen.findByRole("navigation");
  const linksIn = (name: string) =>
    within(within(nav).getByRole("group", { name })).getAllByRole("link").map((link) => link.textContent);
  expect(linksIn("日常")).toEqual(["任务", "待办"]);
  expect(linksIn("搭建")).toEqual(["Agent", "能力", "渠道"]);
  expect(linksIn("管理")).toEqual(["记录", "设置", "平台管理"]);
  expect(within(nav).getByRole("link", { name: "空间概况" })).toBeVisible();
});

test("没有顶栏：账号、语言和主题都在侧栏底部", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  await screen.findByRole("navigation");
  expect(screen.queryByRole("banner")).toBeNull();
  const menu = screen.getByRole("button", { name: "账号与偏好" });
  expect(screen.getByRole("complementary")).toContainElement(menu);
  await userEvent.click(menu);
  // In the document, not toBeVisible: jsdom never finishes the dropdown's
  // enter animation, so a visibility check here measures jsdom, not the menu.
  expect(await screen.findByRole("menuitem", { name: "深色" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "English" })).toBeInTheDocument();
});

const RUN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

function withAgents() {
  return [
    http.get("/api/v1/agents", () =>
      HttpResponse.json([
        { id: "agent-1", name: "Notes tidier", alias: "notes-tidier", status: "active", current_version_id: "v7", created_at: "2026-10-01T00:00:00Z" },
      ]),
    ),
  ];
}

test("⌘K 打开全局搜索，按名字跳到设置里的一段", async () => {
  // 原来要先知道「密钥」在「设置」下面才找得到；搜索按页面和分段的名字找。
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, withAgents());
  await screen.findByRole("navigation");
  await userEvent.keyboard("{Meta>}k{/Meta}");
  const dialog = await screen.findByRole("dialog", { name: "搜索或跳转" });
  await userEvent.type(within(dialog).getByRole("combobox"), "密钥");
  expect(within(dialog).getByRole("option", { name: /设置.*密钥/ })).toBeInTheDocument();
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByTestId("where")).toHaveTextContent(`/workspaces/${WORKSPACE}/settings#secrets`);
});

test("全局搜索能按名字找到 Agent", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, withAgents());
  await userEvent.click(await screen.findByRole("button", { name: /搜索或跳转/ }));
  const dialog = await screen.findByRole("dialog", { name: "搜索或跳转" });
  await userEvent.type(within(dialog).getByRole("combobox"), "notes");
  await userEvent.click(await within(dialog).findByRole("option", { name: /Notes tidier/ }));
  expect(await screen.findByTestId("where")).toHaveTextContent(`/workspaces/${WORKSPACE}/agents/agent-1`);
});

test("粘贴完整的任务 ID 可以直接打开那个任务", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, withAgents());
  await userEvent.click(await screen.findByRole("button", { name: /搜索或跳转/ }));
  const dialog = await screen.findByRole("dialog", { name: "搜索或跳转" });
  await userEvent.type(within(dialog).getByRole("combobox"), RUN);
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByTestId("where")).toHaveTextContent(`/workspaces/${WORKSPACE}/runs/${RUN}`);
});

test("全局搜索不列这个角色看不见的分段", async () => {
  // 和侧栏同一个判据：隐藏不是权限，但不邀请一次注定失败的点击。
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, withAgents());
  // Added after renderShell so they win over its admin defaults: msw prefers
  // the handler registered last, and the first request is still in flight.
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json({ ...ADMIN, is_platform_admin: false })),
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "viewer" })),
  );
  await screen.findByRole("navigation");
  await userEvent.click(await screen.findByRole("button", { name: /搜索或跳转/ }));
  const dialog = await screen.findByRole("dialog", { name: "搜索或跳转" });
  await userEvent.type(within(dialog).getByRole("combobox"), "密钥");
  expect(within(dialog).queryByRole("option", { name: /密钥/ })).toBeNull();
  expect(within(dialog).getByText("没有找到")).toBeInTheDocument();
});
