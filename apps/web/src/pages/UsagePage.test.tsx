import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { UsagePage } from "./UsagePage";
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

function summary(overrides: object = {}) {
  return {
    window: "all_time",
    by_cost_quality: [
      {
        cost_quality: "provider",
        consumed_cost: "12.340000",
        cost_currency: "USD",
        run_count: 3,
        consumed_model_calls: 9,
        consumed_tool_calls: 2,
        consumed_tokens: 4_500,
        consumed_execution_ms: 61_000,
      },
      {
        cost_quality: "unknown",
        consumed_cost: null,
        cost_currency: null,
        run_count: 1,
        consumed_model_calls: 1,
        consumed_tool_calls: 0,
        consumed_tokens: 200,
        consumed_execution_ms: 5_000,
      },
    ],
    total_run_count: 4,
    total_model_calls: 10,
    total_tool_calls: 2,
    total_tokens: 4_700,
    total_execution_ms: 66_000,
    ...overrides,
  };
}

function renderUsage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/usage`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/usage" element={<UsagePage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a provider figure and an unknown one render as two separate rows, not one blended total", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/usage", () => HttpResponse.json(summary())),
  );

  renderUsage();

  // The real cost appears, tagged with where it came from.
  expect(await screen.findByText(/12.340000/)).toBeVisible();
  expect(screen.getByText(/来自服务商|From the provider/i)).toBeVisible();
  // The unpriced bucket says "unknown" in words rather than showing a 0 —
  // the same rule a single Run's cost already follows.
  expect(screen.getByText(/未知|^Unknown$/i)).toBeVisible();
  expect(screen.queryByText(/^0$/)).toBeNull();
});

test("the page never renders a single blended cost outside the per-quality rows", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/usage", () => HttpResponse.json(summary())),
  );

  renderUsage();

  await screen.findByText(/12.340000/);
  // A blended total would be some third money figure nowhere in the
  // fixture — 12.34 + 0 has nothing else to add, so there is no such number
  // to accidentally render. What this pins is structural: the only cost
  // strings on the page are the two the fixture supplies.
  const moneyLike = screen.getAllByText(/\d+\.\d{2,}/);
  expect(moneyLike.map((node) => node.textContent)).toEqual(
    expect.arrayContaining([expect.stringContaining("12.340000")]),
  );
  expect(moneyLike).toHaveLength(1);
});

test("the all-time window is stated, not left for the reader to assume", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/usage", () => HttpResponse.json(summary())),
  );

  renderUsage();

  await screen.findByText(/12.340000/);
  expect(screen.getByText(/从创建以来|since it was created/i)).toBeVisible();
});

test("no usage yet says so instead of an empty table with no explanation", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/usage", () =>
      HttpResponse.json(
        summary({
          by_cost_quality: [],
          total_run_count: 0,
          total_model_calls: 0,
          total_tool_calls: 0,
          total_tokens: 0,
          total_execution_ms: 0,
        }),
      ),
    ),
  );

  renderUsage();

  expect(await screen.findByText(/还没有用量数据|no usage yet/i)).toBeVisible();
});
