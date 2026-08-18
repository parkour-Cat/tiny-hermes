import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { OutboundScopePage } from "./OutboundScopePage";
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

const MEMBER = { ...ADMIN, id: "u2", subject: "dev@example.com", is_platform_admin: false };

function entry(overrides: object = {}) {
  return {
    id: "e1",
    level: "platform",
    workspace_id: null,
    entry: "*.example.com",
    note: null,
    managed: false,
    created_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function renderScopes(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/outbound`]}>
          <AuthProvider>
            <Routes>
              <Route
                path="/workspaces/:workspaceId/outbound"
                element={<OutboundScopePage />}
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a workspace administrator sees the platform range they choose inside", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(MEMBER)),
    http.get("/api/v1/outbound-scopes/platform", () => HttpResponse.json([entry()])),
    http.get("/api/v1/outbound-scopes/workspace", () => HttpResponse.json([])),
  );

  renderScopes();

  // Visible even though they may not change it: a range you cannot see is a
  // range you guess at.
  expect(await screen.findByText("*.example.com")).toBeInTheDocument();
  expect(screen.getByText("本工作空间还没有选中任何目标")).toBeInTheDocument();
});

test("only a platform administrator is offered the platform form", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(MEMBER)),
    http.get("/api/v1/outbound-scopes/platform", () => HttpResponse.json([entry()])),
    http.get("/api/v1/outbound-scopes/workspace", () => HttpResponse.json([])),
  );

  renderScopes();

  await screen.findByText("*.example.com");
  // One form, the workspace's. Absent rather than disabled, the same choice
  // the platform skills make.
  expect(screen.getAllByRole("button", { name: "批准" })).toHaveLength(1);
});

test("a workspace choosing outside the platform range is told which rule stopped it", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(MEMBER)),
    http.get("/api/v1/outbound-scopes/platform", () => HttpResponse.json([entry()])),
    http.get("/api/v1/outbound-scopes/workspace", () => HttpResponse.json([])),
    http.post("/api/v1/outbound-scopes/workspace", () =>
      HttpResponse.json(
        {
          code: "outbound_entry_outside_platform",
          title: "Outside the approved range",
          detail:
            "payments.other.example is not inside anything a platform administrator has approved.",
        },
        { status: 422 },
      ),
    ),
  );

  renderScopes();
  await userEvent.type(
    await screen.findByLabelText("目标"),
    "payments.other.example",
  );
  await userEvent.click(screen.getByRole("button", { name: "批准" }));

  expect(
    await screen.findByText(/is not inside anything a platform administrator/),
  ).toBeInTheDocument();
});

test("an entry a model endpoint owns is labelled and cannot be removed here", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/outbound-scopes/platform", () =>
      HttpResponse.json([entry({ id: "e2", entry: "models.example.com", managed: true })]),
    ),
    http.get("/api/v1/outbound-scopes/workspace", () => HttpResponse.json([])),
  );

  renderScopes();

  expect(await screen.findByText("由模型接入自动批准")).toBeInTheDocument();
  // No remove control: the endpoint would put the entry straight back, so a
  // button here could only ever lose.
  expect(screen.queryByRole("button", { name: "移除" })).not.toBeInTheDocument();
});

test("removing a target warns that agents relying on it will start failing", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let removed = false;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(MEMBER)),
    http.get("/api/v1/outbound-scopes/platform", () => HttpResponse.json([entry()])),
    http.get("/api/v1/outbound-scopes/workspace", () =>
      HttpResponse.json(
        removed
          ? []
          : [
              entry({
                id: "w1",
                level: "workspace",
                workspace_id: WORKSPACE,
                entry: "api.example.com",
              }),
            ],
      ),
    ),
    http.delete("/api/v1/outbound-scopes/w1", () => {
      removed = true;
      return new HttpResponse(null, { status: 204 });
    }),
  );

  renderScopes();
  await userEvent.click(await screen.findByRole("button", { name: "移除" }));

  expect(await screen.findByText(/下一次连接就会被拒绝/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() =>
    expect(screen.getByText("本工作空间还没有选中任何目标")).toBeInTheDocument(),
  );
});

test("a platform with nothing approved says that nothing can be sent", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/outbound-scopes/platform", () => HttpResponse.json([])),
    http.get("/api/v1/outbound-scopes/workspace", () => HttpResponse.json([])),
  );

  renderScopes();

  const platform = (await screen.findByText("平台批准")).closest(".ant-card");
  expect(
    within(platform as HTMLElement).getByText(/现在什么都发不出去/),
  ).toBeInTheDocument();
});
