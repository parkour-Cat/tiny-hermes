import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { InboxPage } from "./InboxPage";
import { AuthProvider } from "../auth/AuthProvider";
import { t } from "../i18n/zh-CN";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

function renderInbox(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/inbox`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/inbox" element={<InboxPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("待办通过二级入口切换队列，每次只显示一个", async () => {
  server.use(
    http.get("/api/v1/auth/me", () =>
      HttpResponse.json({
        id: "u1",
        subject: "admin@example.com",
        display_name: "Admin",
        status: "active",
        is_platform_admin: true,
      }),
    ),
    http.get("/api/v1/workspaces/:id/members/me", () =>
      HttpResponse.json({ role: "workspace_admin" }),
    ),
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [], has_more: false })),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
    http.get("/api/v1/memories/shared", () => HttpResponse.json([])),
    http.get("/api/v1/agents", () => HttpResponse.json([])),
  );
  renderInbox();

  expect(await screen.findByRole("heading", { name: t("inboxUnified"), level: 4 })).toBeVisible();
  expect(screen.queryByRole("heading", { name: t("proposals"), level: 4 })).toBeNull();
  await userEvent.click(screen.getByRole("link", { name: t("proposals") }));
  expect(await screen.findByRole("heading", { name: t("proposals"), level: 4 })).toBeVisible();
  await userEvent.click(screen.getByRole("link", { name: t("memoryReview") }));
  expect(await screen.findByRole("heading", { name: t("memoryReview"), level: 4 })).toBeVisible();
});

test("the default inbox puts all three actionable kinds in one list", async () => {
  const workspace = "11111111-2222-4333-8444-555555555555";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json({ id: "u1", is_platform_admin: true })),
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [{ id: "a1", tool: "email.send", approval_type: "governance_approval", status: "pending", expires_at: "2026-09-06", decided_at: null, document: { target: "recipient" } }], has_more: false })),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([{ id: "p1", name: "New reporting skill", skill_id: null, status: "pending", created_at: "2026-09-06" }])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([{ id: "m1", body: "Remember agreed date", status: "pending", created_at: "2026-09-06" }])),
  );
  render(<TestTheme><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AuthProvider><MemoryRouter initialEntries={[`/workspaces/${workspace}/inbox`]}><Routes><Route path="/workspaces/:workspaceId/inbox" element={<InboxPage />} /></Routes></MemoryRouter></AuthProvider></QueryClientProvider></TestTheme>);
  expect(await screen.findByText("email.send")).toBeVisible();
  expect(await screen.findByText("New reporting skill")).toBeVisible();
  expect(await screen.findByText("Remember agreed date")).toBeVisible();
  expect(screen.getAllByRole("link", { name: "查看并处理" })).toHaveLength(3);
});

test("memory history filters on the server before paging and names the outcome", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json({ id: "u1", is_platform_admin: true })),
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [], has_more: false })),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
    http.get("/api/v1/memories", ({ request }) => HttpResponse.json({ items: new URL(request.url).searchParams.get("status") === "rejected" ? [{ id: "old", body: "Older rejected memory", kind: "private", status: "rejected", updated_at: "2026-09-06" }] : [], has_more: false })),
  );
  renderInbox();
  await userEvent.click(await screen.findByText("处理历史"));
  expect(await screen.findByText("Older rejected memory")).toBeVisible();
  expect(screen.getByText("已拒绝")).toBeVisible();
});
