import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { LoginPage } from "./LoginPage";
import { AuthProvider } from "../auth/AuthProvider";
import { LocaleProvider } from "../i18n/locale";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

function renderLogin(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <LocaleProvider>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/login"]}>
            <AuthProvider>
              <LoginPage />
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </LocaleProvider>
    </TestTheme>,
  );
}

test("a configured provider is offered as a button naming it", async () => {
  server.use(
    http.get("/api/v1/auth/oidc/available", () =>
      HttpResponse.json([{ id: "p1", issuer: "https://accounts.example.com" }]),
    ),
  );

  renderLogin();

  // The issuer is on the button because it is the only thing distinguishing
  // one provider from another — a bare "Sign in with SSO" is a coin flip
  // when a deployment trusts two.
  const button = await screen.findByRole("link", { name: /accounts\.example\.com/ });
  expect(button).toHaveAttribute("href", "/api/v1/auth/oidc/p1/start");
});

test("no configured provider means no SSO section at all", async () => {
  server.use(http.get("/api/v1/auth/oidc/available", () => HttpResponse.json([])));

  renderLogin();

  // Local login must still be there — §218 item 11 is "local accounts AND
  // OIDC", not a choice between them.
  expect(await screen.findByLabelText(/邮箱|email/i)).toBeVisible();
  // And no empty divider or dangling "or" left behind by a section with
  // nothing in it.
  await waitFor(() => {
    expect(screen.queryByText(/^或$|^or$/)).toBeNull();
  });
});

test("a failed callback says so instead of returning a blank login page", async () => {
  server.use(http.get("/api/v1/auth/oidc/available", () => HttpResponse.json([])));

  render(
    <TestTheme>
      <LocaleProvider>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <MemoryRouter initialEntries={["/login?sso_error=1"]}>
            <AuthProvider>
              <LoginPage />
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </LocaleProvider>
    </TestTheme>,
  );

  // Without this the redirect back from a refused callback is
  // indistinguishable from arriving at the login page normally, and the
  // person retries the same broken thing forever.
  expect(await screen.findByText(/没有完成|did not complete/)).toBeVisible();
});
