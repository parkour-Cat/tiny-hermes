import { expect, test, type BrowserContext } from "@playwright/test";
import { CONSOLE_STATE } from "./session";

async function headers(context: BrowserContext, workspace: string) {
  const csrf = (await context.cookies()).find((cookie) => cookie.name === "tiny_hermes_csrf");
  return { "X-Workspace-Id": workspace, "X-CSRF-Token": csrf?.value ?? "" };
}

for (const role of ["workspace_admin", "developer", "viewer"] as const) {
  test(`${role} sees only usable workspace actions and the API enforces the same role`, async ({ browser, baseURL }) => {
    test.skip(process.env.TINY_HERMES_E2E_ROLE_FIXTURES !== "isolated-ux-only", "Seed fixtures/seed_roles.py in an isolated stack first");
    const admin = await browser.newContext({ baseURL, storageState: CONSOLE_STATE });
    const actor = await browser.newContext({ baseURL });
    try {
      const name = `Roles-${role}-${Date.now()}`;
      const created = await admin.request.post("/api/v1/workspaces", {
        headers: await headers(admin, ""), data: { name },
      });
      expect(created.status()).toBe(201);
      const workspace = (await created.json()).id as string;
      const invited = await admin.request.post(`/api/v1/workspaces/${workspace}/members`, {
        headers: await headers(admin, workspace), data: { email: `ux-${role}@example.com`, role },
      });
      expect(invited.status()).toBe(201);
      const login = await actor.request.post("/api/v1/auth/sessions", {
        data: { subject: `ux-${role}@example.com`, password: "ux-role-check-only-123" },
      });
      expect(login.status()).toBe(201);
      const page = await actor.newPage();
      await page.goto(`/workspaces/${workspace}/agents`);
      const create = page.getByRole("button", { name: "新建 Agent", exact: true });
      await expect(create).toBeVisible();
      if (role === "viewer") await expect(create).toBeDisabled();
      else await expect(create).toBeEnabled();
      await expect(page.getByRole("link", { name: "平台管理", exact: true })).toHaveCount(0);
      const operation = await actor.request.post("/api/v1/agents", {
        headers: await headers(actor, workspace), data: { name: "Role check", alias: `role-check-${Date.now()}` },
      });
      expect(operation.status()).toBe(role === "viewer" ? 403 : 201);
      if (role !== "viewer") {
        const agent = await operation.json() as { id: string };
        page.on("dialog", (dialog) => dialog.type() === "beforeunload" ? dialog.accept() : dialog.dismiss());
        await page.goto(`/workspaces/${workspace}/agents/${agent.id}`);
        await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeEnabled();
        const personality = page.getByLabel("人格", { exact: true });
        await personality.fill(`${role} 的未保存修改`);
        await page.reload();
        await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeEnabled();
        await expect(personality).toHaveValue(`${role} 的未保存修改`);
        await page.getByRole("button", { name: "重新载入草稿", exact: true }).click();
        await page.getByRole("button", { name: "确定", exact: true }).click();
        await expect(personality).not.toHaveValue(`${role} 的未保存修改`);
      }
      await page.getByRole("link", { name: "设置", exact: true }).click();
      await page.getByRole("link", { name: "成员", exact: true }).click();
      const invite = page.getByRole("button", { name: "邀请成员", exact: true });
      if (role === "workspace_admin") await expect(invite).toBeEnabled();
      else await expect(invite).toBeDisabled();
      const createWorkspace = await actor.request.post("/api/v1/workspaces", {
        headers: await headers(actor, workspace), data: { name: "Should be refused" },
      });
      expect(createWorkspace.status()).toBe(403);
    } finally {
      await actor.close();
      await admin.close();
    }
  });
}
