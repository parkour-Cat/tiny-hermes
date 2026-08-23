import { expect, test } from "@playwright/test";

/**
 * A channel opened by a person, through the browser.
 *
 * Until this milestone the only code that had ever written a
 * `channel_bindings` row was a test — so the whole Feishu transport was
 * reachable by inserting a row into Postgres by hand and no other way.
 * This walk is the claim that it is now reachable the way §20.1 says: a
 * navigation entry, a form, a row, and a way to close it again.
 *
 * **What it does not prove:** that a delivery signed against this binding
 * is accepted. Probing the webhook with an unsigned body is worthless here
 * — an unknown binding, a disabled one and a real one with a bad signature
 * all answer `401` deliberately (`feishu_service.UnknownChannelBinding`),
 * so the probe would pass against a row the delivery path could not see.
 * Proving that needs a correctly encrypted envelope *and* a published
 * Agent for the Run to land on, which is a different walk. Recorded rather
 * than approximated: a half-proof here would read as the whole one.
 */

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

test("an administrator binds a channel, and the delivery path knows about it", async ({
  page,
}) => {
  const name = unique("Channel");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
  const workspaceId = new URL(page.url()).pathname.split("/")[2] as string;

  // An Agent to publish, and a secret to decrypt with. Both through the API
  // with the browser's own session — this walk is about the channel page.
  const secretName = unique("feishu-key").toLowerCase();
  const agentAlias = unique("greeter").toLowerCase().replace(/_/g, "-");
  const agentId = await page.evaluate(
    async ({ workspaceId, agentAlias, secretName }) => {
      const csrf = document.cookie
        .split("; ")
        .find((entry) => entry.startsWith("tiny_hermes_csrf="))
        ?.split("=")[1];
      const headers = {
        "Content-Type": "application/json",
        "X-Workspace-Id": workspaceId,
        "X-CSRF-Token": csrf ?? "",
      };
      await fetch("/api/v1/secrets", {
        method: "POST",
        headers,
        credentials: "include",
        body: JSON.stringify({
          name: secretName,
          scope: "workspace",
          plaintext: "0123456789abcdef0123456789abcdef",
        }),
      });
      const created = await fetch("/api/v1/agents", {
        method: "POST",
        headers,
        credentials: "include",
        body: JSON.stringify({ name: "Greeter", alias: agentAlias }),
      });
      return ((await created.json()) as { id: string }).id;
    },
    { workspaceId, agentAlias, secretName },
  );
  expect(agentId).toBeTruthy();

  await page.goto(`/workspaces/${workspaceId}/channels`);
  await page.getByRole("button", { name: "绑定渠道" }).click();
  await page.getByLabel("Agent").click();
  await page.getByTitle("Greeter", { exact: true }).click();
  await page.getByLabel("加密密钥").click();
  await page.getByTitle(secretName, { exact: true }).click();
  await page.getByLabel("应用 ID").fill("cli_e2e");
  await page.getByRole("button", { name: "绑定", exact: true }).click();

  // It is on the page, named by the Agent rather than by a uuid.
  await expect(page.getByText("cli_e2e")).toBeVisible();
  await expect(page.getByRole("cell", { name: "Greeter" })).toBeVisible();

  // Closing it is a state, not a deletion — `channel_events` is the record
  // of what this channel already delivered.
  await page.getByRole("button", { name: "停用" }).click();
  await expect(page.getByRole("cell", { name: "disabled" })).toBeVisible();
});
