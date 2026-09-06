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

  expect(await screen.findByRole("heading", { name: t("approvals"), level: 4 })).toBeVisible();
  expect(screen.queryByRole("heading", { name: t("proposals"), level: 4 })).toBeNull();
  await userEvent.click(screen.getByRole("link", { name: t("proposals") }));
  expect(await screen.findByRole("heading", { name: t("proposals"), level: 4 })).toBeVisible();
  await userEvent.click(screen.getByRole("link", { name: t("memoryReview") }));
  expect(await screen.findByRole("heading", { name: t("memoryReview"), level: 4 })).toBeVisible();
});
