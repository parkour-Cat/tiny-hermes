import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { MemoryPage } from "./MemoryPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";

function memory(overrides: object = {}) {
  return {
    id: "m1",
    workspace_id: WORKSPACE,
    agent_id: AGENT,
    kind: "shared",
    status: "pending",
    body: "The Q3 pricing review moved to October.",
    origin: "agent_proposal",
    created_at: "2026-08-22T00:00:00Z",
    updated_at: "2026-08-22T00:00:00Z",
    ...overrides,
  };
}

const AGENTS = [
  { id: AGENT, name: "Support", alias: "support", status: "active", current_version_id: "v1", created_at: "2026-08-01T00:00:00Z" },
];

function renderMemory(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/memory`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/memory" element={<MemoryPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a pending memory is shown as the Agent proposed it, word for word", async () => {
  // The whole risk of a review screen: approving a summary. What gets
  // written into an Agent's shared memory is this text, so this text is
  // what a reviewer has to read — not a shortened version of it.
  server.use(
    http.get("/api/v1/memories/pending", () => HttpResponse.json([memory()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderMemory();

  expect(await screen.findByText("The Q3 pricing review moved to October.")).toBeVisible();
  // And whose memory it would become — an Agent's shared memory is read by
  // every Run of that Agent, so "which Agent" is half the decision.
  expect(screen.getByText("Support")).toBeVisible();
});

test("approving sends the decision and nothing else", async () => {
  // Deliberately not an edit-then-approve: §16.3's approval binds to what
  // was proposed, and the same principle holds here. A console that could
  // alter the body would be writing its own memory under a review the
  // Agent's proposal never received.
  let path: string | null = null;
  let body: string | null = null;
  server.use(
    http.get("/api/v1/memories/pending", () => HttpResponse.json([memory()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.post("/api/v1/memories/m1/approve", async ({ request }) => {
      path = new URL(request.url).pathname;
      body = await request.text();
      return HttpResponse.json(memory({ status: "active" }));
    }),
  );

  renderMemory();
  await userEvent.click(await screen.findByRole("button", { name: /^批准$|^Approve$/ }));

  await waitFor(() => expect(path).toBe("/api/v1/memories/m1/approve"));
  expect(body).toBe("");
});

test("rejecting is offered too, so a queue can be worked to empty", async () => {
  let path: string | null = null;
  server.use(
    http.get("/api/v1/memories/pending", () => HttpResponse.json([memory()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.post("/api/v1/memories/m1/reject", ({ request }) => {
      path = new URL(request.url).pathname;
      return HttpResponse.json(memory({ status: "rejected" }));
    }),
  );

  renderMemory();
  await userEvent.click(await screen.findByRole("button", { name: /^拒绝$|^Reject$/ }));

  await waitFor(() => expect(path).toBe("/api/v1/memories/m1/reject"));
});

test("an empty queue says nothing is waiting, not that memory is off", async () => {
  server.use(
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderMemory();

  expect(await screen.findByText(/没有等待|Nothing is waiting/i)).toBeVisible();
});

test("a role that may not review is told so", async () => {
  // §4.6 gives memory review to an administrator. An empty queue would tell
  // a developer their Agents have proposed nothing.
  server.use(
    http.get("/api/v1/memories/pending", () =>
      HttpResponse.json({ code: "forbidden", detail: "" }, { status: 403 }),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderMemory();

  expect(await screen.findByText(/没有权限|not allowed|forbidden/i)).toBeVisible();
});
