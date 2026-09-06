import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TestTheme } from "../test/TestTheme";
import { ApprovalsPage } from "../pages/ApprovalsPage";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { AuthProvider } from "../auth/AuthProvider";
import { server } from "../test/server";
import { useInboxCount } from "./useInboxCount";

const W = "11111111-2222-4333-8444-555555555555";
function Count({ action = false }: { action?: boolean }) { const count = useInboxCount(); return <><output aria-label="count">{count === null ? "loading" : String(count)}</output>{action ? <ApprovalsPage /> : null}</>; }
function setup(role: string, more = false, fail = false, action = false) {
  let decided = false;
  const calls: string[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json({ id: "u", is_platform_admin: false })),
    http.get(`/api/v1/workspaces/${W}/members/me`, () => HttpResponse.json({ role })),
    http.get("/api/v1/skills", () => HttpResponse.json([{ id: "s1", scope: "workspace" }, { id: "s2", scope: "platform" }])),
    http.get("/api/v1/approvals", ({ request }) => {
      const q = new URL(request.url).searchParams;
      calls.push(`approvals:${q.get("approval_type")}`);
      return HttpResponse.json({ items: decided ? [] : [{ id: "a", run_id: "r1", tool: "http.orders.create", document: { target: "https://example.com/orders", arguments: {} }, expires_at: "2026-12-01T00:00:00Z", status: "pending", approval_type: "governance_approval" }], has_more: more });
    }),
    http.post("/api/v1/approvals/a/decision", () => { decided = true; return HttpResponse.json({ id: "a", status: "approved" }); }),
    http.get("/api/v1/skill-proposals", () => {
      calls.push("proposals");
      return fail ? new HttpResponse(null, { status: 503 }) : HttpResponse.json([
        { id: "p1", status: "pending", skill_id: "s1" },
        { id: "p2", status: "pending", skill_id: "s2" },
      ]);
    }),
    http.get("/api/v1/memories/pending", () => { calls.push("memories"); return HttpResponse.json([{ id: "m" }]); }),
  );
  render(<TestTheme><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[`/workspaces/${W}`]}><AuthProvider><Routes><Route path="/workspaces/:workspaceId" element={<Count action={action} />} /></Routes></AuthProvider></MemoryRouter></QueryClientProvider></TestTheme>);
  return calls;
}
test("developer counts only proposals they can decide, without requesting admin queues", async () => {
  const calls = setup("developer");
  await waitFor(() => expect(screen.getByLabelText("count")).toHaveTextContent("1"));
  expect(calls).toEqual(["proposals"]);
});
test("viewer has no actionable count and makes no queue requests", async () => {
  const calls = setup("viewer");
  await waitFor(() => expect(screen.getByLabelText("count")).toHaveTextContent("0"));
  expect(calls).toEqual([]);
});
test("administrator counts governance, workspace proposals and memory with a lower-bound marker", async () => {
  const calls = setup("workspace_admin", true);
  await waitFor(() => expect(screen.getByLabelText("count")).toHaveTextContent("3+"));
  expect(calls).toContain("approvals:governance_approval");
});
test("a failed eligible queue shows unknown rather than a misleading partial count", async () => {
  setup("workspace_admin", false, true);
  await waitFor(() => expect(screen.getByLabelText("count")).toHaveTextContent("?"));
});

test("deciding a real queue item refreshes the sidebar count immediately", async () => {
  document.cookie = "tiny_hermes_csrf=test-token";
  setup("workspace_admin", false, false, true);
  await waitFor(() => expect(screen.getByLabelText("count")).toHaveTextContent("3"));
  await userEvent.click(await screen.findByRole("button", { name: "批准" }));
  await userEvent.click(await screen.findByRole("button", { name: "确定" }));
  await waitFor(() => expect(screen.getByLabelText("count")).toHaveTextContent("2"));
});
