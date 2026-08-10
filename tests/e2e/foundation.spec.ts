import { expect, test } from "@playwright/test";

import { ADMIN } from "./session";

test("login, create two workspaces and logout", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("邮箱").fill(ADMIN.subject);
  await page.getByLabel("密码").fill(ADMIN.password);
  await page.getByRole("button", { name: "登录" }).click();

  // Counted against what this account already has rather than against zero:
  // other specs sign in as the same administrator and leave workspaces behind,
  // and "two more than before" is the fact this test is actually about.
  await expect(page).toHaveURL(/\/workspaces$/);
  await expect(page.getByRole("button", { name: "新建工作空间" })).toBeVisible();
  const before = await page.getByRole("listitem").count();

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

  await expect(page.getByRole("listitem")).toHaveCount(before + 2);
  await page.reload();
  await expect(page.getByText(firstName, { exact: true })).toBeVisible();
  await expect(page.getByText(secondName, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
});
