import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { IdentityProvidersPage } from "./IdentityProvidersPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

function provider(overrides: object = {}) {
  return {
    id: "p1",
    issuer: "https://login.example.com",
    client_id: "tiny-hermes",
    client_secret_ref: "OIDC_CLIENT_SECRET",
    discovery_url: "https://login.example.com/.well-known/openid-configuration",
    scopes: ["openid", "email"],
    status: "enabled",
    created_by: "u1",
    created_at: "2026-08-22T00:00:00Z",
    ...overrides,
  };
}

function renderProviders(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/identity-providers`]}>
          <Routes>
            <Route
              path="/workspaces/:workspaceId/identity-providers"
              element={<IdentityProvidersPage />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a registered provider is listed by its issuer", async () => {
  server.use(http.get("/api/v1/oidc/providers", () => HttpResponse.json([provider()])));

  renderProviders();

  expect(await screen.findByText("https://login.example.com")).toBeVisible();
});

test("the form sends a reference, never a client secret", async () => {
  // The same rule `ModelEndpointRow.credential_ref` follows: this console
  // never holds the plaintext, so there is no field here that could carry
  // one and no request body that could log one.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/oidc/providers", () => HttpResponse.json([])),
    http.post("/api/v1/oidc/providers", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(provider(), { status: 201 });
    }),
  );

  renderProviders();
  await userEvent.click(await screen.findByRole("button", { name: /注册身份提供方|Register/i }));
  await userEvent.type(screen.getByLabelText(/签发者|Issuer/i), "https://idp.example.com");
  await userEvent.type(screen.getByLabelText(/客户端 ID|Client ID/i), "th");
  await userEvent.type(screen.getByLabelText(/客户端密钥引用|Client secret reference/i), "IDP_SECRET");
  await userEvent.type(
    screen.getByLabelText(/发现地址|Discovery/i),
    "https://idp.example.com/.well-known/openid-configuration",
  );
  await userEvent.click(screen.getByRole("button", { name: /^注册$|^Register$/ }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({
    issuer: "https://idp.example.com",
    client_id: "th",
    client_secret_ref: "IDP_SECRET",
    discovery_url: "https://idp.example.com/.well-known/openid-configuration",
    scopes: ["openid", "email", "profile"],
  });
});

test("a disabled provider stays listed and is not offered a second disable", async () => {
  // Disabling is how a deployment stops trusting an IdP. Hiding the row
  // would leave no way to see that this deployment once did.
  server.use(
    http.get("/api/v1/oidc/providers", () =>
      HttpResponse.json([provider({ status: "disabled" })]),
    ),
  );

  renderProviders();

  expect(await screen.findByText("https://login.example.com")).toBeVisible();
  expect(screen.queryByRole("button", { name: /停用|Disable/ })).toBeNull();
});

test("a non-platform administrator is told, not shown an empty list", async () => {
  // Registering an IdP is instance-wide (§21's second wizard step), so the
  // route is platform-admin only. An empty table would say this deployment
  // trusts nobody, which is a different claim.
  server.use(
    http.get("/api/v1/oidc/providers", () =>
      HttpResponse.json({ code: "forbidden", detail: "" }, { status: 403 }),
    ),
  );

  renderProviders();

  expect(await screen.findByText(/没有权限|not allowed|forbidden/i)).toBeVisible();
});

test("客户端密钥引用这一列不整列显示长引用", async () => {
  // §4.1：表格里的 ID 与引用名一律截断，完整值挂在 title 上。
  server.use(
    http.get("/api/v1/oidc/providers", () =>
      HttpResponse.json([provider({ client_secret_ref: "oidc-client-secret-9f2c4b7a1d3e5f60" })]),
    ),
  );
  renderProviders();
  const cell = await screen.findByTitle("oidc-client-secret-9f2c4b7a1d3e5f60");
  expect(cell.textContent).not.toContain("1d3e5f60");
});
