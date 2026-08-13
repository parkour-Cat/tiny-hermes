import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { MembersPage } from "./MembersPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const USER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

function member(overrides: Record<string, unknown> = {}) {
  return {
    user_id: USER,
    display_name: "Admin",
    subject: "admin@example.com",
    role: "workspace_admin",
    ...overrides,
  };
}

function renderMembers(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/members`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/members" element={<MembersPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("the member list shows who is already here", async () => {
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE}/members`, () => HttpResponse.json([member()])),
  );

  renderMembers();

  expect(await screen.findByText("admin@example.com")).toBeInTheDocument();
  expect(screen.getByText("Admin")).toBeInTheDocument();
});

test("inviting posts the email of an existing user", async () => {
  const sent: unknown[] = [];
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE}/members`, () => HttpResponse.json([member()])),
    http.post(`/api/v1/workspaces/${WORKSPACE}/members`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(
        member({
          user_id: "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
          display_name: "Dev",
          subject: "dev@example.com",
          role: "developer",
        }),
        { status: 201 },
      );
    }),
  );

  renderMembers();
  await userEvent.type(await screen.findByLabelText("邮箱"), "dev@example.com");
  await userEvent.click(screen.getByRole("button", { name: "邀请成员" }));

  await waitFor(() =>
    expect(sent).toEqual([{ email: "dev@example.com", role: "developer" }]),
  );
  expect(await screen.findByText("dev@example.com")).toBeInTheDocument();
});

test("an unknown email is the platform's error, not an implicit signup", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE}/members`, () => HttpResponse.json([member()])),
    http.post(`/api/v1/workspaces/${WORKSPACE}/members`, () =>
      HttpResponse.json(
        {
          code: "user_not_found",
          detail: "No registered user has that email. Inviting does not create an account.",
        },
        { status: 404 },
      ),
    ),
  );

  renderMembers();
  await userEvent.type(await screen.findByLabelText("邮箱"), "missing@example.com");
  await userEvent.click(screen.getByRole("button", { name: "邀请成员" }));

  expect(
    await screen.findByText("没有用该邮箱注册的用户，邀请不会自动创建账号"),
  ).toBeInTheDocument();
});
