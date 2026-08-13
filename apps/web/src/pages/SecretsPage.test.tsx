import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { SecretsPage } from "./SecretsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const SECRET = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const PLAINTEXT = "sk-secret-value";

const ADMIN = {
  id: "u1",
  subject: "admin@example.com",
  display_name: "Admin",
  status: "active",
  is_platform_admin: true,
};

function renderSecrets(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/secrets`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/secrets" element={<SecretsPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("plaintext typed into create is not echoed back from the list", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  const listed: Record<string, unknown>[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/secrets", () => HttpResponse.json(listed)),
    http.post("/api/v1/secrets", async ({ request }) => {
      const body = (await request.json()) as { name: string; plaintext: string };
      expect(body.plaintext).toBe(PLAINTEXT);
      const created = {
        id: SECRET,
        workspace_id: WORKSPACE,
        name: body.name,
        scope: "workspace",
        status: "active",
        mask: "sk••••ue",
        created_at: "2026-08-13T00:00:00Z",
        updated_at: "2026-08-13T00:00:00Z",
      };
      listed.push(created);
      return HttpResponse.json(created, { status: 201 });
    }),
  );

  renderSecrets();
  await userEvent.type(await screen.findByLabelText("名称"), "openai");
  await userEvent.type(screen.getByLabelText("明文"), PLAINTEXT);
  await userEvent.click(screen.getByRole("button", { name: "创建" }));

  expect(await screen.findByText("sk••••ue")).toBeInTheDocument();
  expect(screen.getByText("openai")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByDisplayValue(PLAINTEXT)).not.toBeInTheDocument());
  expect(screen.queryByText(PLAINTEXT)).not.toBeInTheDocument();
});

test("a platform administrator can start a rewrap", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(ADMIN)),
    http.get("/api/v1/secrets", () => HttpResponse.json([])),
    http.post("/api/v1/secrets/rewrap", () =>
      HttpResponse.json({ processed: 2, remaining: 0, current_key_id: "v2" }),
    ),
  );

  renderSecrets();
  await userEvent.click(await screen.findByRole("button", { name: "重包" }));
  expect(await screen.findByText(/2/)).toBeInTheDocument();
});
