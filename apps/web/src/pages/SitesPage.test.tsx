import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import type { Role } from "../workspace/useMyRole";
import { WorkspacePermissions } from "../workspace/WorkspacePermissions";
import { SitesPage } from "./SitesPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const SITE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const FUTURE = "2099-01-01T00:00:00Z";

const ACTIVE = {
  id: SITE,
  run_id: "cccccccc-dddd-4eee-8fff-000000000000",
  title: "Ledger",
  root: "site",
  visibility: "members",
  created_at: "2026-10-25T00:00:00Z",
  expires_at: FUTURE,
  revoked_at: null,
  active: true,
};

function renderSites(role: Role): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <WorkspacePermissions role={role}>
          <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/sites`]}>
            <Routes>
              <Route path="/workspaces/:workspaceId/sites" element={<SitesPage />} />
            </Routes>
          </MemoryRouter>
        </WorkspacePermissions>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a published site is listed by title, who can open it and when it ends, without its link", async () => {
  server.use(http.get("/api/v1/sites", () => HttpResponse.json([ACTIVE, { ...ACTIVE, id: "x", title: null, active: false, revoked_at: "2026-10-25T01:00:00Z" }])));
  renderSites("viewer");

  expect(await screen.findByText("Ledger")).toBeInTheDocument();
  expect(screen.getByText("仅成员")).toBeInTheDocument();
  expect(screen.getByText("已撤销")).toBeInTheDocument();
  expect(screen.getByText("site")).toBeInTheDocument();
  expect(screen.queryByText(/\/s\//)).toBeNull();
});

test("a viewer may revoke but not extend or open a site to anyone", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let revoked = false;
  server.use(
    http.get("/api/v1/sites", () => HttpResponse.json(revoked ? [{ ...ACTIVE, active: false, revoked_at: FUTURE }] : [ACTIVE])),
    http.post(`/api/v1/sites/${SITE}/revoke`, () => {
      revoked = true;
      return new HttpResponse(null, { status: 204 });
    }),
  );
  renderSites("viewer");

  await screen.findByText("Ledger");
  expect(screen.queryByRole("button", { name: "延长 7 天" })).toBeNull();
  expect(screen.queryByRole("button", { name: "改为任何人可打开" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "撤销" }));
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "撤销" }));

  await waitFor(() => expect(revoked).toBe(true));
  expect(await screen.findByText("已撤销")).toBeInTheDocument();
});

test("a developer may extend, and only an administrator opens a site to anyone with the link", async () => {
  server.use(http.get("/api/v1/sites", () => HttpResponse.json([ACTIVE])));
  renderSites("developer");
  await screen.findByText("Ledger");
  expect(screen.getByRole("button", { name: "延长 7 天" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "改为任何人可打开" })).toBeNull();
});

test("an administrator is asked before a site is opened to anyone with the link", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let opened: unknown = null;
  server.use(
    http.get("/api/v1/sites", () => HttpResponse.json([ACTIVE])),
    http.put(`/api/v1/sites/${SITE}/visibility`, async ({ request }) => {
      opened = await request.json();
      return HttpResponse.json({ visibility: "link" });
    }),
  );
  renderSites("workspace_admin");

  await userEvent.click(await screen.findByRole("button", { name: "改为任何人可打开" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/拿到链接的任何人/)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: "确定" }));

  await waitFor(() => expect(opened).toEqual({ visibility: "link" }));
});

test("with no sites yet, the page says how one is made", async () => {
  server.use(http.get("/api/v1/sites", () => HttpResponse.json([])));
  renderSites("viewer");

  expect(await screen.findByText("还没有发布的网站")).toBeInTheDocument();
  expect(screen.getByText(/site\.publish/)).toBeInTheDocument();
});
