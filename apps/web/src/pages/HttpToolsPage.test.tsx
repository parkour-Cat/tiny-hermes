import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { HttpToolsPage } from "./HttpToolsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

const ADMIN = {
  id: "u1",
  subject: "admin@example.com",
  display_name: "Admin",
  status: "active",
  is_platform_admin: true,
};

function tool(overrides: object = {}) {
  return {
    id: "t1",
    workspace_id: WORKSPACE,
    name: "orders",
    base_url: "https://api.example.com/v2",
    credential_ref: null,
    current_version_id: "v1",
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function version(overrides: object = {}) {
  return {
    id: "v1",
    http_tool_id: "t1",
    version_number: 1,
    content_hash: "abc",
    title: "Orders",
    document_version: "1",
    operations: [
      {
        operation_id: "listOrders",
        method: "GET",
        path: "/orders",
        summary: "List them.",
        read_only: true,
      },
      {
        operation_id: "createOrder",
        method: "POST",
        path: "/orders",
        summary: "Place one.",
        read_only: false,
      },
    ],
    status: "active",
    bindable: true,
    created_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function renderTools(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/http-tools`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/http-tools" element={<HttpToolsPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("an operation that writes is marked where somebody is choosing", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/http-tools", () => HttpResponse.json([tool()])),
    http.get("/api/v1/http-tools/t1/versions", () => HttpResponse.json([version()])),
  );

  renderTools();

  // Whether a call will stop for a person is what you need to know while
  // binding, not afterwards.
  expect(await screen.findByText(/GET listOrders/)).toBeInTheDocument();
  expect(screen.getByText(/POST createOrder · 会改数据/)).toBeInTheDocument();
});

test("a host the workspace never approved is refused with the host named", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/http-tools", () => HttpResponse.json([])),
    http.post("/api/v1/http-tools", () =>
      HttpResponse.json(
        {
          type: "https://tiny-hermes.dev/errors/host-outside-workspace-scope",
          code: "host_outside_workspace_scope",
          title: "Host not approved",
          status: 422,
          detail:
            "api.example.com is not in this workspace's outbound scope. A workspace administrator approves it under Outbound scope first.",
        },
        { status: 422 },
      ),
    ),
  );

  renderTools();

  await userEvent.type(await screen.findByLabelText("名称"), "orders");
  await userEvent.type(screen.getByLabelText("基础地址"), "https://api.example.com");
  await userEvent.type(screen.getByLabelText("OpenAPI 文档"), "{{}}");
  await userEvent.click(screen.getByRole("button", { name: "登记" }));

  // The host and the page that grants it, so the reader is sent to the person
  // who can approve rather than to the code.
  expect(await screen.findAllByText(/api\.example\.com is not in this workspace/)).not.toHaveLength(
    0,
  );
});

test("withdrawing a version asks first and says what still works", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  const withdrawn: string[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/http-tools", () => HttpResponse.json([tool()])),
    http.get("/api/v1/http-tools/t1/versions", () => HttpResponse.json([version()])),
    http.post("/api/v1/http-tools/t1/versions/v1/withdraw", () => {
      withdrawn.push("v1");
      return HttpResponse.json(version({ status: "withdrawn", bindable: false }));
    }),
  );

  renderTools();

  await userEvent.click(await screen.findByRole("button", { name: "停用该版本" }));
  expect(
    await screen.findByText("新的绑定会被拒绝。已经绑了这个版本的 Agent 照常运行。"),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(withdrawn).toEqual(["v1"]));
});

test("nothing registered says so", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/http-tools", () => HttpResponse.json([])),
  );

  renderTools();

  expect(await screen.findByText("还没有登记 HTTP 工具。")).toBeInTheDocument();
});
