import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ModelEndpointsPage } from "./ModelEndpointsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const ENDPOINT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

const SUMMARY = {
  id: ENDPOINT,
  name: "acme-gpt",
  model: "acme-large",
  context_window: 128000,
  max_output_tokens: 4096,
  usage_quality: "provider",
  status: "active",
};

function user(admin: boolean) {
  return {
    id: "u1",
    subject: "user@example.com",
    display_name: "User",
    status: "active",
    is_platform_admin: admin,
  };
}

function renderEndpoints(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/model-endpoints`]}>
          <AuthProvider>
            <Routes>
              <Route
                path="/workspaces/:workspaceId/model-endpoints"
                element={<ModelEndpointsPage />}
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a platform administrator sees the base url and whether a credential exists", async () => {
  let details = 0;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () => {
      details += 1;
      return HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      });
    }),
  );

  renderEndpoints();

  expect(await screen.findByText("https://models.example.com/v1")).toBeInTheDocument();
  expect(screen.getByText("凭证已配置")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "注册端点" })).toBeInTheDocument();
  await waitFor(() => expect(details).toBe(1));
});

test("everyone else lists the summary and never the base url", async () => {
  let details = 0;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(false))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () => {
      details += 1;
      return HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      });
    }),
  );

  renderEndpoints();

  expect(await screen.findByText("acme-gpt")).toBeInTheDocument();
  expect(screen.queryByText("https://models.example.com/v1")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "注册端点" })).not.toBeInTheDocument();
  expect(details).toBe(0);
});
