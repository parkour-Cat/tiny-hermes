import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { InboxPage } from "./InboxPage";

test("the default inbox puts all three actionable kinds in one list", async () => {
  const workspace = "11111111-2222-4333-8444-555555555555";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json({ id: "u1", is_platform_admin: true })),
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [{ id: "a1", tool: "email.send", approval_type: "governance_approval", status: "pending", created_at: "2026-09-06", document: { target: "recipient" } }], has_more: false })),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([{ id: "p1", name: "New reporting skill", skill_id: null, status: "pending", created_at: "2026-09-06" }])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([{ id: "m1", body: "Remember agreed date", status: "pending", created_at: "2026-09-06" }])),
  );
  render(<TestTheme><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AuthProvider><MemoryRouter initialEntries={[`/workspaces/${workspace}/inbox`]}><Routes><Route path="/workspaces/:workspaceId/inbox" element={<InboxPage />} /></Routes></MemoryRouter></AuthProvider></QueryClientProvider></TestTheme>);
  expect(await screen.findByText("email.send")).toBeVisible();
  expect(await screen.findByText("New reporting skill")).toBeVisible();
  expect(await screen.findByText("Remember agreed date")).toBeVisible();
  expect(screen.getAllByRole("link", { name: "查看并处理" })).toHaveLength(3);
});
