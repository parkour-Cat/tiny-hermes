import path from "node:path";

import type { Locator, Page } from "@playwright/test";

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
 * navigation, then the section's anchor. Returns the section, so a spec can
 * scope its labels to it — every section of a group is on the same page, and
 * 「名称」 alone now matches three forms.
 */
export async function openSection(
  page: Page,
  group: string,
  section: string,
  id: string,
): Promise<Locator> {
  await page.getByRole("link", { name: group, exact: true }).click();
  await page.getByRole("link", { name: section, exact: true }).click();
  const found = page.locator(`section#${id}`);
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
