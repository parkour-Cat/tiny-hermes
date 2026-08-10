import { expect, test } from "@playwright/test";

const bootstrapToken =
  process.env.TINY_HERMES_E2E_BOOTSTRAP_TOKEN ??
  "local-bootstrap-token-with-32-characters";

test("bootstrap, login, create two workspaces and logout", async ({ page, request }) => {
  const bootstrap = await request.post("/api/v1/bootstrap", {
    headers: { "X-Bootstrap-Token": bootstrapToken },
    data: {
      subject: "admin@example.com",
      display_name: "Admin",
      password: "long-pass-123",
    },
  });
  expect(bootstrap.status()).toBe(201);

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("admin@example.com");
  await page.getByLabel("密码").fill("long-pass-123");
  await page.getByRole("button", { name: "登录" }).click();

  const suffix = Date.now();
  const firstName = `Workspace-${suffix}-A`;
  const secondName = `Workspace-${suffix}-B`;
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(firstName);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByText(firstName, { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "新建工作空间" })).toBeHidden();

  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(secondName);
  await page.getByRole("button", { name: "创建", exact: true }).click();

  await expect(page.getByRole("listitem")).toHaveCount(2);
  await page.reload();
  await expect(page.getByText(firstName, { exact: true })).toBeVisible();
  await expect(page.getByText(secondName, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
});
