import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ChatHomePage } from "./ChatHomePage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const USER = {
  id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  subject: "admin@example.com",
  display_name: "林清",
  status: "active",
  is_platform_admin: true,
};

function renderHome(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/chat"]}>
          <AuthProvider>
            <Routes>
              <Route path="/chat" element={<ChatHomePage />} />
              <Route path="/chat/:workspaceId/agents/:agentId" element={<p>opened chat</p>} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("lists published agents and hides unpublished ones", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/workspaces", () =>
      HttpResponse.json([{ id: WORKSPACE, name: "Acme 运营", status: "active" }]),
    ),
    http.get("/api/v1/agents", () =>
      HttpResponse.json([
        {
          id: "22222222-3333-4444-8555-666666666666",
          name: "值班分析员",
          alias: "analyst",
          status: "published",
          current_version_id: "v1",
          created_at: "2026-08-10T00:00:00Z",
        },
        {
          id: "33333333-4444-4555-8666-777777777777",
          name: "草稿员",
          alias: "drafty",
          status: "draft",
          current_version_id: null,
          created_at: "2026-08-10T00:00:00Z",
        },
      ]),
    ),
  );

  renderHome();

  expect(await screen.findByRole("link", { name: "值班分析员" })).toHaveAttribute(
    "href",
    `/chat/${WORKSPACE}/agents/22222222-3333-4444-8555-666666666666`,
  );
  expect(screen.queryByText("草稿员")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "成员" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "机密" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "API 密钥" })).not.toBeInTheDocument();
});

test("opening a published agent lands on the chat route", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/workspaces", () =>
      HttpResponse.json([{ id: WORKSPACE, name: "Acme 运营", status: "active" }]),
    ),
    http.get("/api/v1/agents", () =>
      HttpResponse.json([
        {
          id: "22222222-3333-4444-8555-666666666666",
          name: "值班分析员",
          alias: "analyst",
          status: "published",
          current_version_id: "v1",
          created_at: "2026-08-10T00:00:00Z",
        },
      ]),
    ),
  );

  renderHome();
  await userEvent.click(await screen.findByRole("link", { name: "值班分析员" }));
  expect(screen.getByText("opened chat")).toBeInTheDocument();
});
