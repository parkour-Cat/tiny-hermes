import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * The console, driven the way a person drives it, against the real stack.
 *
 * Nothing here is seeded and no route exists only for this file: the Agent is
 * created and published through the forms, the Run is executed by the Worker
 * container, and the timeline fills from the event stream the API serves.
 */

/** A name nothing else in the stack will have. */
function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

async function openWorkspace(page: Page): Promise<void> {
  const name = unique("Console");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
}

/**
 * Picks a value from an Ant Design select.
 *
 * By the option's `title` rather than by its role: rc-select renders a second,
 * screen-reader-only option list that carries the same role and is never
 * visible, and a role query finds that one first.
 */
async function choose(page: Page, label: string, value: string): Promise<void> {
  await page.getByLabel(label).click();
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator(`.ant-select-item-option[title="${value}"]`)
    .click();
}

/**
 * Binds a tool the way a person does: the visible Ant Design wrapper.
 *
 * `locator.check()` targets the opacity-0 native input. Clicking that input
 * does not change Ant Design 6's checked state, so the walk never saves a
 * bound tool.
 */
async function bindTool(page: Page, name: string): Promise<void> {
  const box = page.getByRole("checkbox", { name });
  await expect(box).not.toBeChecked();
  await page.locator(".ant-checkbox-wrapper").filter({ hasText: name }).click();
  await expect(box).toBeChecked();
}

/** Creates an Agent, writes the scenario into its draft, and publishes v1. */
async function publishAgent(page: Page, scenario: string): Promise<string> {
  const name = unique(scenario);
  await page.getByRole("link", { name: "智能体", exact: true }).click();
  await page.getByRole("button", { name: "新建智能体" }).click();
  await page.getByLabel("名称").fill(name);
  // The platform's alias grammar is lowercase words joined by hyphens, so a
  // scenario name with an underscore in it cannot be an alias unchanged.
  await page.getByLabel("别名").fill(name.toLowerCase().replace(/_/g, "-"));
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "新建智能体" })).toBeHidden();
  await page.getByRole("link", { name, exact: true }).click();

  await expect(page.getByText("草稿修订 1")).toBeVisible();
  await page.getByRole("textbox", { name: "人格" }).fill(`A ${scenario} agent for the console acceptance walk.`);
  await choose(page, "模型场景", scenario);
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿修订 2")).toBeVisible();

  await page.getByRole("button", { name: "发布" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  // Read back rather than assumed: the version number is the platform's answer
  // to the publish, and it is the thing a later Run will execute.
  await expect(page.getByText("当前版本 v1")).toBeVisible();
  return name;
}

/** Submits a Run for the named Agent and lands on its detail page. */
async function submitRun(page: Page, agentName: string): Promise<string> {
  await page.getByRole("link", { name: "运行", exact: true }).click();
  await page.getByRole("button", { name: "提交运行" }).click();
  await choose(page, "智能体", agentName);
  await page.getByLabel("输入").fill("Say hello to the acceptance walk.");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  return page.url().split("/").pop() as string;
}

/** The 概要 card, so a status is read where the page states it. */
function summary(page: Page) {
  return page.locator(".ant-card", { hasText: "概要" }).first();
}

function timeline(page: Page) {
  return page.locator(".ant-card", { hasText: "时间线" }).first();
}

/** Every sequence number the timeline is showing, in the order shown. */
async function sequences(page: Page): Promise<number[]> {
  const texts = await timeline(page).locator(".ant-timeline-item-content").allInnerTexts();
  return texts.flatMap((text) => {
    const found = /#(\d+)/.exec(text);
    return found === null ? [] : [Number(found[1])];
  });
}

test("draft, publish, submit, watch, retry, and be refused a foreign workspace", async ({
  page,
}) => {
  await openWorkspace(page);
  const workspaceUrl = new URL(page.url());
  const agent = await publishAgent(page, "continue_once");
  const runId = await submitRun(page, agent);

  // The first event arrives over the stream, on the page that submitted the
  // Run. Nothing has been reloaded at this point.
  await expect(timeline(page).locator(".ant-timeline-item")).not.toHaveCount(0);

  // One reload mid-Run: the resume path, triggered the way a user triggers it.
  // The subscription starts again from the beginning, so what follows must be
  // the whole history rather than the part that happened after the reload.
  await page.reload();

  await expect(summary(page).getByText("已完成", { exact: true })).toBeVisible();
  await expect(timeline(page).getByText("run_completed")).toBeVisible();

  const shown = await sequences(page);
  expect(shown.length).toBeGreaterThan(1);
  // Contiguous from the first event and each one once: a resumed stream that
  // skipped or repeated a frame would show it here.
  expect(shown).toEqual(Array.from({ length: shown.length }, (_, index) => index + 1));
  // A gap marker is what an unrecoverable history looks like. This Run is
  // minutes old, so seeing one would mean the console invented the gap.
  await expect(timeline(page).getByText("无法再取回")).toHaveCount(0);

  // A failure that is safe to replay, retried from the button the platform
  // offers rather than from one the console decided to show.
  const failing = await publishAgent(page, "fail_replay_safe");
  const failedRun = await submitRun(page, failing);
  await expect(summary(page).getByText("失败", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "重试运行" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  // A retry is a new Run under the same budget, so the page moves rather than
  // reporting something about the one that failed.
  await expect(page).not.toHaveURL(new RegExp(`${failedRun}$`));
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);

  // The same Run under a Workspace this session has no standing in. The console
  // sends the header the address implies and shows what comes back; it does not
  // check membership itself, and it does not quietly substitute a workspace it
  // knows about.
  const foreign = "00000000-0000-4000-8000-000000000000";
  await page.goto(`${workspaceUrl.origin}/workspaces/${foreign}/runs/${runId}`);
  await expect(page.getByText("No such run exists in the selected workspace.")).toBeVisible();
  await expect(summary(page)).toHaveCount(0);
});

test("the panes phase two cannot fill are absent, not empty", async ({ page }) => {
  await openWorkspace(page);
  const agent = await publishAgent(page, "complete");
  await submitRun(page, agent);
  await expect(summary(page)).toBeVisible();

  // Phase three and four. A pane reading "暂无数据" would tell the user that
  // nothing happened, which is a different claim from "not built yet".
  for (const absent of ["父子任务树", "上下文和压缩事件", "Token 和费用"]) {
    await expect(page.getByText(absent)).toHaveCount(0);
  }
});

test("the builder binds a tool, playground sends, and rollback restores v1", async ({ page }) => {
  await openWorkspace(page);
  const name = unique("playground");
  await page.getByRole("link", { name: "智能体", exact: true }).click();
  await page.getByRole("button", { name: "新建智能体" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByLabel("别名").fill(name.toLowerCase().replace(/_/g, "-"));
  await page.getByRole("button", { name: "创建" }).click();
  await expect(page.getByRole("dialog", { name: "新建智能体" })).toBeHidden();
  await page.getByRole("link", { name, exact: true }).click();

  await page.getByRole("textbox", { name: "人格" }).fill("A playground agent for the console acceptance walk.");
  await choose(page, "模型场景", "continue_once");
  await page.getByRole("tab", { name: "工具" }).click();
  await bindTool(page, "file.list");
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿修订 2")).toBeVisible();
  await page.getByRole("button", { name: "发布" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  await expect(page.getByText("当前版本 v1")).toBeVisible();

  await page.getByRole("button", { name: "打开试验场" }).click();
  await expect(page).toHaveURL(/\/playground$/);
  await page.getByLabel("输入要发给智能体的消息").fill("Say hello to the playground walk.");
  await page.getByRole("button", { name: "发送" }).click();
  // A bound tool yields a tool-call turn and a final turn, so there are two
  // role tags. One assistant message is the claim; uniqueness is not.
  await expect(page.getByText("助手", { exact: true }).first()).toBeVisible({
    timeout: 60_000,
  });

  const pause = page.getByRole("button", { name: "暂停" });
  if (await pause.isVisible().catch(() => false)) {
    await pause.click();
    await page.getByLabel("输入要发给智能体的消息").fill("A second turn while the head is paused.");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText("当前会话被队列挡住")).toBeVisible();
  }

  await page.getByRole("link", { name }).click();
  await page.getByRole("textbox", { name: "人格" }).fill("A second published voice.");
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿修订 3")).toBeVisible();
  await page.getByRole("button", { name: "发布" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  await expect(page.getByText("当前版本 v2")).toBeVisible();
  await page.getByRole("button", { name: "回滚到此版本" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  await expect(page.getByText("当前版本 v1")).toBeVisible();
});

test("the locale switcher changes chrome and can switch back", async ({ page }) => {
  await openWorkspace(page);
  await expect(page.getByRole("link", { name: "运行" })).toBeVisible();
  await page.getByLabel("语言").click();
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator('.ant-select-item-option[title="English"]')
    .click();
  await expect(page.getByRole("link", { name: "Runs" })).toBeVisible();
  await page.getByLabel("Language").click();
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator('.ant-select-item-option[title="中文"]')
    .click();
  await expect(page.getByRole("link", { name: "运行" })).toBeVisible();
});
