import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { theme } from "antd";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test } from "vitest";

import { ConsoleLayout } from "./ConsoleLayout";
import { ConsoleTheme, consoleDesignToken } from "./ConsoleTheme";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
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

function renderShell(path: string, hits = { count: 0 }): { hits: { count: number } } {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    countingWorkspaces(hits),
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

  expect(await screen.findByRole("link", { name: "智能体" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/agents`,
  );
  expect(screen.getByRole("link", { name: "运行" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/runs`,
  );
  expect(screen.getByRole("link", { name: "成员" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/members`,
  );
  expect(screen.getByRole("link", { name: "模型端点" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/model-endpoints`,
  );
  expect(screen.getByRole("link", { name: "API 密钥" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/api-keys`,
  );
  expect(screen.getByRole("link", { name: "机密" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/secrets`,
  );
  expect(document.querySelector(".th-sider")).not.toBeNull();
  expect(document.querySelector("svg.th-hermes-mark")).not.toBeNull();
  expect(document.querySelector(".th-mark")).toBeNull();
  expect(document.querySelector(".th-topbar")?.querySelector("a.th-nav-link")).toBeNull();
  expect(screen.queryByRole("link", { name: "Approvals" })).not.toBeInTheDocument();
  expect(screen.queryByText("审批")).not.toBeInTheDocument();
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

  const dark = theme.getDesignToken({
    algorithm: theme.darkAlgorithm,
    token: consoleDesignToken(true),
  });
  expect(screen.getByTestId("container")).toHaveTextContent(dark.colorBgContainer);
});

test("a light system preference keeps the default algorithm", () => {
  renderThemed(<ThemeProbe />);

  const light = theme.getDesignToken({
    algorithm: theme.defaultAlgorithm,
    token: consoleDesignToken(false),
  });
  expect(screen.getByTestId("container")).toHaveTextContent(light.colorBgContainer);
});

test("a stored dark theme wins over a light system preference", () => {
  window.localStorage.setItem("tiny-hermes-theme", "dark");

  renderThemed(<ThemeProbe />);

  const dark = theme.getDesignToken({
    algorithm: theme.darkAlgorithm,
    token: consoleDesignToken(true),
  });
  expect(screen.getByTestId("container")).toHaveTextContent(dark.colorBgContainer);
});

test("the locale switcher changes chrome into English", async () => {
  renderShell(`/workspaces/${WORKSPACE}/agents`);

  expect(await screen.findByRole("link", { name: "运行" })).toBeInTheDocument();
  await userEvent.click(screen.getByLabelText("语言"));
  await userEvent.click(await screen.findByTitle("English"));

  expect(await screen.findByRole("link", { name: "Runs" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Members" })).toBeInTheDocument();
});
