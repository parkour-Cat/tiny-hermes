import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { theme } from "antd";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test } from "vitest";

import { ConsoleLayout } from "./ConsoleLayout";
import { ConsoleTheme } from "./ConsoleTheme";
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
    ["工具与技能", "tooling"],
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
  await userEvent.click(screen.getByRole("button", { name: "退出" }));

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

  const dark = theme.getDesignToken({ algorithm: theme.darkAlgorithm });
  expect(screen.getByTestId("container")).toHaveTextContent(dark.colorBgContainer);
});

test("a light system preference keeps the default algorithm", () => {
  renderThemed(<ThemeProbe />);

  const light = theme.getDesignToken({ algorithm: theme.defaultAlgorithm });
  expect(screen.getByTestId("container")).toHaveTextContent(light.colorBgContainer);
});

test("a stored dark theme wins over a light system preference", () => {
  window.localStorage.setItem("tiny-hermes-theme", "dark");

  renderThemed(<ThemeProbe />);

  const dark = theme.getDesignToken({ algorithm: theme.darkAlgorithm });
  expect(screen.getByTestId("container")).toHaveTextContent(dark.colorBgContainer);
});

test("the locale switcher changes chrome into English", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);

  expect(await screen.findByRole("link", { name: "任务" })).toBeInTheDocument();
  await userEvent.click(screen.getByLabelText("语言"));
  await userEvent.click(await screen.findByTitle("English"));

  expect(await screen.findByRole("link", { name: "Runs" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Settings/ })).toBeInTheDocument();
});

/** The three queues the inbox badge adds up, all empty unless a test says
 *  otherwise. Declared last so a test's own handler for one of them wins. */
function emptyQueues() {
  return [
    http.get("/api/v1/approvals", () => HttpResponse.json([])),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
  ];
}

test("导航上是七个入口，不是十八个", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);
  const nav = await screen.findByRole("navigation");
  expect(within(nav).getAllByRole("link")).toHaveLength(7);
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
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, [
    http.get("/api/v1/approvals", () => HttpResponse.json([{ id: "a" }, { id: "b" }])),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([{ id: "c" }])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
  ]);
  expect(await screen.findByText("3")).toBeVisible();
});

test("有一个队列读不到时不显示数字", async () => {
  // 显示「2」而其实是「2 + 读不到」，比不显示更糟：它看起来是个准确的数。
  renderShell(`/workspaces/${WORKSPACE}/agents`, { count: 0 }, [
    http.get("/api/v1/approvals", () => HttpResponse.json([{ id: "a" }, { id: "b" }])),
    http.get("/api/v1/skill-proposals", () => new HttpResponse(null, { status: 403 })),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
  ]);
  await screen.findByRole("navigation");
  await waitFor(() => expect(screen.queryByText("2")).toBeNull());
});
