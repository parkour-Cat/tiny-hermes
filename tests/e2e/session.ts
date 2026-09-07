import path from "node:path";

import { expect, type Locator, type Page } from "@playwright/test";

/**
 * The one account a stack is bootstrapped with, and where its cookies are kept.
 *
 * Shared from a plain module rather than from `bootstrap.setup.ts`: importing a
 * file that registers a test would register it a second time in whichever
 * project imported it.
 */
export const ADMIN = {
  subject: "admin@example.com",
  displayName: "Admin",
  password: "long-pass-123",
};

export const BOOTSTRAP_TOKEN =
  process.env.TINY_HERMES_E2E_BOOTSTRAP_TOKEN ?? "local-bootstrap-token-with-32-characters";

/** Where the setup project leaves the signed-in browser state. */
export const CONSOLE_STATE = path.join(__dirname, ".auth", "console.json");

/**
 * Walks to one section of a grouped page the way a person does since the
 * console went from eighteen entries to seven: the group's entry in the
 * navigation, then the section tab. The accessible region remains stable
 * when anchor ids are removed to prevent scroll jumps.
 */
export async function openSection(
  page: Page,
  group: string,
  section: string,
  _id: string,
): Promise<Locator> {
  await page.getByRole("link", { name: group, exact: true }).click();
  await page.getByRole("link", { name: section, exact: true }).click();
  const found = page.getByRole("region", { name: section, exact: true });
  await found.waitFor();
  return found;
}

/**
 * Unfolds a section of a builder form if it is folded. A folded section keeps
 * its fields in the DOM but hidden, and a hidden checkbox is not one a person
 * — or a role query — can reach.
 */
export async function unfold(page: Page, title: string): Promise<void> {
  const header = page.getByRole("button", { name: new RegExp(title) }).first();
  if ((await header.getAttribute("aria-expanded")) !== "true") await header.click();
}

/** Keyboard selection avoids clicking an option while the popup is moving. */
export async function selectAntOption(page: Page, label: string, value: string): Promise<void> {
  const field = page.getByLabel(label, { exact: true });
  await field.click();
  if (await field.evaluate((element) => !(element as HTMLInputElement).readOnly)) {
    await field.fill(value);
  }
  // A previous popup can remain mounted during its closing animation.
  const listId = await field.getAttribute("aria-controls");
  expect(listId).toBeTruthy();
  const popup = page.locator(".ant-select-dropdown").filter({
    has: page.locator(`[id="${listId}"]`),
  });
  await expect(popup.locator(`.ant-select-item-option[title="${value}"]`)).toBeVisible();
  const active = popup.locator(".ant-select-item-option-active");
  const count = await popup.locator(".ant-select-item-option").count();
  for (let step = 0; step <= count; step++) {
    if (await active.count() > 0 && await active.getAttribute("title") === value) break;
    await field.press("ArrowDown");
  }
  await expect(active).toHaveAttribute("title", value);
  await field.press("Enter");
  const select = field.locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " ant-select ")][1]');
  await expect(select).toContainText(value);
  // Multiple-select controls keep the list open after accepting one value.
  await field.press("Escape");
  await expect(field).toHaveAttribute("aria-expanded", "false");
}
