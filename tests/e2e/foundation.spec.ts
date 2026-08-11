import { expect, test } from "@playwright/test";

import { ADMIN } from "./session";

test("login, create two workspaces and logout", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("邮箱").fill(ADMIN.subject);
  await page.getByLabel("密码").fill(ADMIN.password);
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page).toHaveURL(/\/workspaces$/);
  await expect(page.getByRole("button", { name: "新建工作空间" })).toBeVisible();

  const suffix = Date.now();
  const firstName = `Workspace-${suffix}-A`;
  const secondName = `Workspace-${suffix}-B`;
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(firstName);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByText(firstName, { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "新建工作空间" })).toBeHidden();

  // Counted from here rather than from before the first creation, and not from
  // zero: this administrator is shared with the other specs and already owns
  // whatever they left behind, and a count read before the list has loaded is a
  // count of nothing. What this asserts is that one creation adds one row.
  const before = await page.getByRole("listitem").count();
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(secondName);
  await page.getByRole("button", { name: "创建", exact: true }).click();

  await expect(page.getByRole("listitem")).toHaveCount(before + 1);
  await page.reload();
  await expect(page.getByText(firstName, { exact: true })).toBeVisible();
  await expect(page.getByText(secondName, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
});
