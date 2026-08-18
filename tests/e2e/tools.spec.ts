import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * An external tool's whole life, driven through the console against the real
 * stack — and the one moment this stage exists for: a write that stops.
 *
 * Register an OpenAPI document, bind one read and one write to an Agent,
 * publish, run the read and watch it reach the far end, then run the write and
 * watch the Run stop instead. A person approves it on the Approvals page, and
 * the Run finishes. Every step before the pause is what makes the pause a real
 * claim rather than a unit test's.
 *
 * **The tool points at this platform's own API.** `GET /health/live` is
 * a real endpoint that needs no credential, and the Worker reaches it the same
 * way it would reach anybody else's: out through the egress proxy, measured
 * against the four-layer scope. Using the platform as its own stand-in keeps a
 * fake service out of the production compose file, and costs nothing — what is
 * being tested is the path, not the far end.
 *
 * This walk therefore needs a stack whose egress proxy is configured and whose
 * `OUTBOUND_ALLOWED_CIDRS` covers the compose bridge network. Without that
 * every outbound call refuses, which is M2C-1's whole point; the test says so
 * in its skip message rather than failing with something unrelated.
 */

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

/** Where the tool points, from inside the compose network. */
const TOOL_HOST = "api";
const TOOL_BASE = `http://${TOOL_HOST}:8000`;

/**
 * Two operations on one path: one read and one write.
 *
 * The write is `POST /health/live`, which the API answers 405. That is
 * deliberate and it is enough: what this walk proves is that the platform
 * stopped before sending it, and a 405 afterwards is the far end's business.
 */
const DOCUMENT = JSON.stringify({
  openapi: "3.0.3",
  info: { title: "Platform health", version: "1" },
  paths: {
    "/health/live": {
      get: { operationId: "readLiveness", summary: "Is the API alive." },
      post: { operationId: "pokeLiveness", summary: "Pretend to change it." },
    },
  },
});

async function openWorkspace(page: Page): Promise<string> {
  const name = unique("Tools");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
  return name;
}

/**
 * Picks a value from an Ant Design select, by option title.
 *
 * By `title` rather than by role, for the reason `skills.spec.ts` records:
 * rc-select renders a second, screen-reader-only list carrying the same role.
 */
async function choose(page: Page, label: string, value: string): Promise<void> {
  await page.getByLabel(label).click();
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator(`.ant-select-item-option[title="${value}"]`)
    .click();
}

/** Approves the host at both levels. A workspace may only choose inside the
 *  platform's, so the platform entry has to exist first. */
async function approveHost(page: Page): Promise<void> {
  await page.getByRole("link", { name: "出站范围", exact: true }).click();
  // The two forms by position rather than by their cards' text: each card's
  // prose mentions the other level — that is the point of the page, each layer
  // narrowing the one above — so a text filter matches both. The platform's
  // form is first because that is the order the rule runs in.
  const forms = page.locator("form");
  await expect(forms).toHaveCount(2);
  for (const index of [0, 1]) {
    const form = forms.nth(index);
    await form.getByLabel("目标").fill(TOOL_HOST);
    await form.getByRole("button", { name: "批准" }).click();
    await expect(page.getByText(TOOL_HOST, { exact: true })).toHaveCount(index + 1);
  }
}

async function registerTool(page: Page): Promise<void> {
  await page.getByRole("link", { name: "HTTP 工具", exact: true }).click();
  await page.getByLabel("名称").fill("health");
  await page.getByLabel("基础地址").fill(TOOL_BASE);
  await page.getByLabel("OpenAPI 文档").fill(DOCUMENT);
  await page.getByRole("button", { name: "登记" }).click();
  // Both operations, with the write marked where somebody is choosing.
  await expect(page.getByText(/GET readLiveness/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/POST pokeLiveness · 会改数据/)).toBeVisible();
}

/** Publishes an Agent bound to both operations, with §16.3's choice made. */
async function publishAgent(page: Page, operation: string): Promise<string> {
  const name = unique("caller");
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await page.getByRole("button", { name: "新建 Agent" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByLabel("别名").fill(name.toLowerCase().replace(/_/g, "-"));
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page).toHaveURL(/\/agents\/[0-9a-f-]{36}$/);

  // Wait for the draft before typing: its values arrive as the form's initial
  // values, and anything typed before they do is replaced by them.
  await expect(page.getByText("草稿修订 1")).toBeVisible();
  await page.getByLabel("人格").fill("A tools acceptance agent.");
  await choose(page, "模型场景", "http_once");
  await choose(page, "网络", TOOL_HOST);
  await choose(page, "HTTP 操作", operation);
  // Required for a bound write, and chosen here rather than discovered at
  // publish. `governance` is what makes the Run stop for a person.
  await choose(page, "HTTP 写操作怎么办", "每次都问管理员");
  // Read the form back before saving: a select whose click was swallowed
  // leaves its default in place, and this walk would publish it.
  await expect(page.getByText("http_once", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿修订 2")).toBeVisible();
  await page.getByRole("button", { name: "发布" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  await expect(page.getByText("当前版本 v1")).toBeVisible();
  return name;
}

async function submitRun(page: Page, agentName: string, asked: string): Promise<string> {
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("button", { name: "提交任务" }).click();
  await choose(page, "Agent", agentName);
  // The drill's vocabulary: the input names the call to make.
  await page.getByLabel("输入").fill(asked);
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  // Returned so the walk can come back to *this* Run after visiting another
  // page. Reaching for "the first link on the runs list" would find the header
  // brand, which is how this step once navigated out of the console entirely.
  return page.url();
}

function timeline(page: Page) {
  return page.locator(".ant-card", { hasText: "时间线" }).first();
}

test("register an HTTP tool, call it, and let a person approve the write", async ({
  page,
}) => {
  test.setTimeout(240_000);
  await openWorkspace(page);
  await approveHost(page);
  await registerTool(page);

  // -- the read goes all the way -----------------------------------------
  const reader = await publishAgent(page, "GET readLiveness");
  await submitRun(page, reader, "http.health.readLiveness");
  await expect(page.getByText("completed", { exact: true }).first()).toBeVisible({
    timeout: 120_000,
  });
  // The far end's own words came back into the conversation — twice over:
  // once as the tool result and once in what the model said about it.
  await expect(page.getByText(/HTTP 200/).first()).toBeVisible();
  await expect(page.getByText(/"status":"alive"/).first()).toBeVisible();

  // -- and the write stops ------------------------------------------------
  const writer = await publishAgent(page, "POST pokeLiveness · 会改数据");
  const writeRun = await submitRun(page, writer, "http.health.pokeLiveness");
  await expect(page.getByText("waiting_approval", { exact: true }).first()).toBeVisible({
    timeout: 120_000,
  });
  await expect(timeline(page).getByText(/这次运行在等人/)).toBeVisible();

  // -- a person reads exactly what would be sent, and decides -------------
  await page.getByRole("link", { name: "审批", exact: true }).click();
  const governance = page.locator(".ant-card", { hasText: "工作空间的决定" }).first();
  // Named twice: once in the summary row and once inside the document, which
  // is the assertion the whole page exists for — a reviewer who cannot see the
  // request cannot approve it. The URL appears only in the document.
  await expect(governance.getByText("http.health.pokeLiveness").first()).toBeVisible();
  await expect(governance.getByText(TOOL_BASE, { exact: false })).toBeVisible();
  await governance.getByRole("button", { name: "批准" }).click();
  await page.getByRole("button", { name: "确定" }).click();

  // -- and the Run finishes the call it was stopped for -------------------
  await page.goto(writeRun);
  await expect(page.getByText("completed", { exact: true }).first()).toBeVisible({
    timeout: 120_000,
  });
});
