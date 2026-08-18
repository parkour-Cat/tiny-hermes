import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ApprovalsPage } from "./ApprovalsPage";
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

function approval(overrides: object = {}) {
  return {
    id: "a1",
    run_id: "r1",
    approval_type: "governance_approval",
    status: "pending",
    tool: "http.orders.createOrder",
    document: {
      tool: "http.orders.createOrder",
      arguments: { sku: "abc-123" },
      target: "https://api.example.com/v2/orders",
      required_permission: "http.orders.write",
    },
    required_permission: "http.orders.write",
    requested_by: "u1",
    expires_at: "2026-08-19T00:00:00Z",
    decided_by: null,
    decided_at: null,
    decision_reason: null,
    ...overrides,
  };
}

function renderApprovals(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/approvals`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/approvals" element={<ApprovalsPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("the two kinds are shown apart, because they are two different powers", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/approvals", () =>
      HttpResponse.json([
        approval(),
        approval({ id: "a2", approval_type: "user_confirmation", tool: "file.write" }),
      ]),
    ),
  );

  renderApprovals();

  // A merged list would invite the reader to work through them as one queue,
  // which is the habit §16.3 exists to prevent.
  // Awaited on the rows rather than on the headings: the headings are static
  // and render before the list has arrived.
  expect(await screen.findAllByText(/http\.orders\.createOrder/)).not.toHaveLength(0);
  expect(screen.getByText("发起运行的那个人")).toBeInTheDocument();
  expect(screen.getByText("工作空间的决定")).toBeInTheDocument();
  expect(screen.getAllByText(/file\.write/).length).toBeGreaterThan(0);
});

test("the arguments shown are the normalized ones the platform hashed", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/approvals", () => HttpResponse.json([approval()])),
  );

  renderApprovals();

  // Rendered rather than summarized: a reviewer deciding from a summary this
  // console rewrote would be approving something nobody can check.
  await screen.findByText(/http\.orders\.createOrder/);
  expect(screen.getByText(/abc-123/)).toBeInTheDocument();
});

test("approving asks first and says what exactly it allows", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  const decided: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/approvals", () => HttpResponse.json([approval()])),
    http.post("/api/v1/approvals/a1/decision", async ({ request }) => {
      decided.push(await request.json());
      return HttpResponse.json(approval({ status: "approved" }));
    }),
  );

  renderApprovals();

  await screen.findByText(/http\.orders\.createOrder/);
  await userEvent.click(screen.getByRole("button", { name: "批准" }));
  expect(await screen.findByText("这只允许这一次调用。换一个调用需要一条新的批准。")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(decided).toEqual([{ decision: "approve", reason: null }]));
});

test("a rejection cannot be sent without a reason", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  const decided: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/approvals", () => HttpResponse.json([approval()])),
    http.post("/api/v1/approvals/a1/decision", async ({ request }) => {
      decided.push(await request.json());
      return HttpResponse.json(approval({ status: "rejected" }));
    }),
  );

  renderApprovals();

  await screen.findByText(/http\.orders\.createOrder/);
  await userEvent.click(screen.getByRole("button", { name: "拒绝" }));
  // Submitting an empty reason: the form refuses before the server has to.
  await userEvent.click(screen.getAllByRole("button", { name: "拒绝" })[1] as HTMLElement);

  expect(await screen.findByText("请填写此项")).toBeInTheDocument();
  expect(decided).toEqual([]);
});

test("a rejection with a reason sends it", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  const decided: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/approvals", () => HttpResponse.json([approval()])),
    http.post("/api/v1/approvals/a1/decision", async ({ request }) => {
      decided.push(await request.json());
      return HttpResponse.json(approval({ status: "rejected" }));
    }),
  );

  renderApprovals();

  await screen.findByText(/http\.orders\.createOrder/);
  await userEvent.click(screen.getByRole("button", { name: "拒绝" }));
  await userEvent.type(await screen.findByRole("textbox"), "not this quarter");
  await userEvent.click(screen.getAllByRole("button", { name: "拒绝" })[1] as HTMLElement);

  await waitFor(() =>
    expect(decided).toEqual([{ decision: "reject", reason: "not this quarter" }]),
  );
});

test("nothing waiting says so in both sections", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/approvals", () => HttpResponse.json([])),
  );

  renderApprovals();

  expect(await screen.findAllByText("没有待处理的审批。")).toHaveLength(2);
});
