import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { McpServersPage } from "./McpServersPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { t } from "../i18n/zh-CN";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

const ADMIN = {
  id: "u1",
  subject: "admin@example.com",
  display_name: "Admin",
  status: "active",
  is_platform_admin: true,
};

function mcpServer(overrides: object = {}) {
  return {
    id: "s1",
    workspace_id: WORKSPACE,
    name: "docs",
    url: "https://mcp.example.com",
    credential_ref: null,
    current_version_id: "v1",
    last_validated_at: "2026-08-18T00:00:00Z",
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function version(overrides: object = {}) {
  return {
    id: "v1",
    mcp_server_id: "s1",
    version_number: 1,
    content_hash: "abc",
    tools: [
      { name: "search", description: "Search the index.", input_schema: {} },
      { name: "purge", description: "Remove everything.", input_schema: {} },
    ],
    status: "active",
    bindable: true,
    created_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function renderServers(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/mcp-servers`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/mcp-servers" element={<McpServersPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a server shows what it advertised and when it was last read", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([mcpServer()])),
    http.get("/api/v1/mcp-servers/s1/versions", () => HttpResponse.json([version()])),
  );

  renderServers();

  expect(await screen.findByText("search")).toBeInTheDocument();
  expect(screen.getByText("purge")).toBeInTheDocument();
  expect(screen.getByText(/上次读取/)).toBeInTheDocument();
});

test("a server this platform has never reached says so", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/mcp-servers", () =>
      HttpResponse.json([mcpServer({ last_validated_at: null })]),
    ),
    http.get("/api/v1/mcp-servers/s1/versions", () => HttpResponse.json([])),
  );

  renderServers();

  // "Registered" and "reachable" are different facts, and only one of them is
  // a promise.
  expect(await screen.findByText(/从未读到过/)).toBeInTheDocument();
});

test("reading an unchanged server says so rather than looking like nothing happened", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([mcpServer()])),
    http.get("/api/v1/mcp-servers/s1/versions", () => HttpResponse.json([version()])),
    // 200: the snapshot was identical, so no version was added.
    http.post("/api/v1/mcp-servers/s1/refresh", () => HttpResponse.json(version())),
  );

  renderServers();

  await userEvent.click(await screen.findByRole("button", { name: "重新读取" }));

  expect(await screen.findByText("没有变化——没有新版本需要审。")).toBeInTheDocument();
});

test("a server that could not be reached is reported and not recorded", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([])),
    http.post("/api/v1/mcp-servers", () =>
      HttpResponse.json(
        {
          type: "https://tiny-hermes.dev/errors/mcp-server-unreachable",
          code: "mcp_server_unreachable",
          title: "Server unreachable",
          status: 502,
          detail: "the target could not be reached",
        },
        { status: 502 },
      ),
    ),
  );

  renderServers();

  await userEvent.type(await screen.findByLabelText("名称"), "docs");
  await userEvent.type(screen.getByLabelText("地址"), "https://mcp.example.com");
  await userEvent.click(screen.getByRole("button", { name: "登记" }));

  await waitFor(() =>
    expect(screen.getByText(/the target could not be reached/)).toBeInTheDocument(),
  );
});

test("nothing registered says so", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([])),
  );

  renderServers();

  expect(await screen.findByText("还没有登记 MCP 服务。")).toBeInTheDocument();
});

test("a bindable version can be withdrawn, the way an HTTP tool version can", async () => {
  // The two catalogues answer the same question — "may an Agent still bind
  // this?" — and only one of them could say no. A server whose tools turned
  // out to be wrong had no way to be taken out of circulation short of
  // deleting it, which takes the record of what it once offered with it.
  let withdrew: string | null = null;
  server.use(
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([mcpServer()])),
    http.get("/api/v1/mcp-servers/s1/versions", () => HttpResponse.json([version()])),
    http.post(
      "/api/v1/mcp-servers/s1/versions/v1/withdraw",
      ({ request }) => {
        withdrew = new URL(request.url).pathname;
        return HttpResponse.json(version({ bindable: false, status: "withdrawn" }));
      },
    ),
  );

  renderServers();
  await userEvent.click(await screen.findByRole("button", { name: t("mcpServerWithdraw") }));
  // Confirmed, like the HTTP tool catalogue's own withdraw: this takes a
  // version out of circulation for every Agent that has not bound it yet.
  await userEvent.click(await screen.findByRole("button", { name: t("confirm") }));

  await waitFor(() =>
    expect(withdrew).toBe("/api/v1/mcp-servers/s1/versions/v1/withdraw"),
  );
});

test("a version already withdrawn is not offered the button again", async () => {
  server.use(
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([mcpServer()])),
    http.get("/api/v1/mcp-servers/s1/versions", () =>
      HttpResponse.json([version({ bindable: false, status: "withdrawn" })]),
    ),
  );

  renderServers();

  await screen.findByText(/withdrawn/);
  expect(screen.queryByRole("button", { name: t("mcpServerWithdraw") })).toBeNull();
});
