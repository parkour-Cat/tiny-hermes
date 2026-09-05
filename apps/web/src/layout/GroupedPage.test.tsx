import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { GroupedPage } from "./GroupedPage";
import { AuthProvider } from "../auth/AuthProvider";
import { t } from "../i18n/zh-CN";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

function renderGrouped(groupKey: string, isPlatformAdmin = false): void {
  server.use(
    http.get("/api/v1/auth/me", () =>
      HttpResponse.json({
        id: "u1",
        subject: "user@example.com",
        display_name: "User",
        status: "active",
        is_platform_admin: isPlatformAdmin,
      }),
    ),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/${groupKey}`]}>
          <AuthProvider>
            <Routes>
              <Route
                path="/workspaces/:workspaceId/:group"
                element={<GroupedPage groupKey={groupKey} render={(key) => <p>section {key}</p>} />}
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("画出这个人看得见的段，不画他看不见的", async () => {
  // 隐藏不是权限——后端照旧拒绝。这里只是不邀请一次注定失败的点击。
  // `members` 对 viewer 是可见的（列成员是 READERS 的事），所以这里用 `secrets`
  // 作为 viewer 看不见的段：/api/v1/secrets 对 viewer 实测 403。
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "viewer" })),
  );
  renderGrouped("settings");

  expect(await screen.findByText("section members")).toBeVisible();
  expect(screen.queryByText("section secrets")).toBeNull();
  expect(screen.queryByText("section identity-providers")).toBeNull();
});

test("角色还没拿到时，一段都不画", () => {
  // 先画全部再删掉看不见的，会让 viewer 看到一次闪现的入口列表。
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => new Promise(() => {})),
  );
  renderGrouped("settings");
  expect(screen.queryByText("section members")).toBeNull();
});

test("平台管理员的段跟着标志走", async () => {
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () =>
      HttpResponse.json({ role: "workspace_admin" }),
    ),
  );
  renderGrouped("settings", true);

  expect(await screen.findByText("section identity-providers")).toBeVisible();
  expect(screen.getByText(t("navSettingsIntro"))).toBeVisible();
});
