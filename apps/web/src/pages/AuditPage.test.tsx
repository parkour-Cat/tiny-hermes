import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { AuditPage } from "./AuditPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

const USER = {
  id: "u1",
  subject: "admin@example.com",
  display_name: "Admin",
  status: "active",
  is_platform_admin: false,
};

function row(overrides: object = {}) {
  return {
    id: "e1",
    workspace_id: WORKSPACE,
    actor_type: "user",
    actor_id: "u9",
    action: "run.paused",
    resource_type: "run",
    resource_id: "r1",
    result: "succeeded",
    request_id: "req-1",
    context: {},
    created_at: "2026-08-22T01:02:03Z",
    ...overrides,
  };
}

function renderAudit(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/audit`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/audit" element={<AuditPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a redacted view says so, because an empty detail column cannot", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/audit-events", () =>
      HttpResponse.json({ items: [row()], has_more: false, visibility: "redacted" }),
    ),
  );

  renderAudit();

  // The row itself is identical to an unredacted row whose context happened
  // to be empty — that is exactly why the banner has to carry the fact. A
  // reader investigating an incident would otherwise read "—" as "nothing
  // was recorded" rather than "you may not see it".
  expect(await screen.findByText(/脱敏|redacted view/i)).toBeVisible();
});

test("a narrowed view says so too, because a short list looks like a quiet one", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/audit-events", () =>
      HttpResponse.json({ items: [row()], has_more: false, visibility: "own_resources" }),
    ),
  );

  renderAudit();

  expect(await screen.findByText(/与你有关|rows you are named in/i)).toBeVisible();
});

test("a full view is not decorated with a warning it does not need", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/audit-events", () =>
      HttpResponse.json({
        items: [row({ context: { reason: "held for review" } })],
        has_more: false,
        visibility: "full",
      }),
    ),
  );

  renderAudit();

  expect(await screen.findByText(/held for review/)).toBeVisible();
  expect(screen.queryByText(/脱敏|redacted view/i)).toBeNull();
  expect(screen.queryByText(/与你有关|rows you are named in/i)).toBeNull();
});
