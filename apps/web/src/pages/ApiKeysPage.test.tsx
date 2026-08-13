import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ApiKeysPage } from "./ApiKeysPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const KEY = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";

const ACCOUNT_ROW = {
  id: ACCOUNT,
  workspace_id: WORKSPACE,
  name: "ci-bot",
  role: "developer",
  status: "active",
  created_by_user_id: "u1",
  created_at: "2026-08-10T00:00:00Z",
};

function renderKeys(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/api-keys`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/api-keys" element={<ApiKeysPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a minted key's plaintext is shown once and then dismissed", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  const token = "thk_plaintext-only-once";
  server.use(
    http.get("/api/v1/service-accounts", () => HttpResponse.json([ACCOUNT_ROW])),
    http.get(`/api/v1/service-accounts/${ACCOUNT}/api-keys`, () => HttpResponse.json([])),
    http.post(`/api/v1/service-accounts/${ACCOUNT}/api-keys`, () =>
      HttpResponse.json(
        {
          id: KEY,
          service_account_id: ACCOUNT,
          prefix: "thk_abcd",
          scopes: ["runs.read", "runs.write", "runs.control", "agents.read"],
          agent_ids: [],
          expires_at: null,
          revoked_at: null,
          created_at: "2026-08-10T01:00:00Z",
          token,
        },
        { status: 201 },
      ),
    ),
  );

  renderKeys();
  await userEvent.click(await screen.findByRole("button", { name: "新建 API 密钥" }));

  expect(await screen.findByText(token)).toBeInTheDocument();
  expect(screen.getByText("thk_abcd")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "我已保存" }));

  await waitFor(() => expect(screen.queryByText(token)).not.toBeInTheDocument());
  expect(screen.getByText("thk_abcd")).toBeInTheDocument();
});
