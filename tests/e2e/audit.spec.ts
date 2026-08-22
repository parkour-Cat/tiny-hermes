import { expect, test } from "@playwright/test";

/**
 * The trail, read by a person, showing an action that person just performed.
 *
 * The claim this walk makes that no test below it can: **the audit an
 * administrator reads is the one the platform actually wrote.** Every test
 * under it either seeds `audit_events` directly or asserts the read path in
 * isolation — so all of them would still pass if the write path and the read
 * path had drifted onto different actions, different resource types, or
 * different workspaces. Here nothing is seeded: a workspace is created
 * through the form, an Agent is created through the API with the browser's
 * own session, and the row that comes back has to be the one that creation
 * wrote.
 *
 * That matters because "the console shows an audit table" and "the console
 * shows what happened" are different claims, and only the second one is
 * worth anything to somebody investigating an incident.
 */

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

test("an administrator sees the action they just took, in the trail", async ({ page }) => {
  const name = unique("Audit");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
  const workspaceId = new URL(page.url()).pathname.split("/")[2] as string;

  // An action worth auditing, taken through the API with the browser's own
  // session so the actor is this signed-in administrator and not a fixture.
  const alias = unique("auditee").toLowerCase().replace(/_/g, "-");
  const created = await page.evaluate(
    async ({ workspaceId, alias }) => {
      const csrf = document.cookie
        .split("; ")
        .find((entry) => entry.startsWith("tiny_hermes_csrf="))
        ?.split("=")[1];
      const response = await fetch("/api/v1/agents", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Workspace-Id": workspaceId,
          ...(csrf === undefined ? {} : { "X-CSRF-Token": csrf }),
        },
        body: JSON.stringify({ name: alias, alias }),
      });
      return { status: response.status, json: await response.json() };
    },
    { workspaceId, alias },
  );
  expect(created.status, JSON.stringify(created.json)).toBe(201);
  const agentId = created.json.id as string;

  await page.goto(`/workspaces/${workspaceId}/audit`);

  // The row for *that* Agent, not merely a non-empty table. An assertion on
  // the table's existence would pass against a page that rendered somebody
  // else's history, or none at all.
  await expect(page.getByText(agentId).first()).toBeVisible({ timeout: 30_000 });

  // A workspace administrator reads the full trail, so neither partial-view
  // banner belongs here. If one of them showed up, the page would be telling
  // this reader their evidence is incomplete when it is not — the mirror of
  // the failure the banner exists to prevent.
  await expect(page.getByText("你看到的是脱敏后的记录")).toBeHidden();
  await expect(page.getByText("你只看到与你有关的记录")).toBeHidden();
});
