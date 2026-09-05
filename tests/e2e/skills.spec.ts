import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { openSection, unfold } from "./session";

/**
 * A skill's whole life, driven through the console against the real stack.
 *
 * Upload it, bind it to an Agent, publish, run — the model loads the document
 * and the timeline says so — then let the Agent propose a change, read the
 * difference, approve it, and check the one thing this stage exists to
 * guarantee: the Agent that proposed the change is still running the version
 * it was published with. That last assertion is the point of the file. Every
 * step before it is what makes it a real claim rather than a unit test's.
 *
 * The Run is submitted from the Runs page rather than the Playground because
 * the timeline lives on the Run detail page, and the timeline is the evidence
 * `skill_loaded` is asserted from.
 */

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

const SKILL_NAME = "rollout";

function skillDocument(line: string): string {
  return [
    "---",
    `name: ${SKILL_NAME}`,
    "description: How this company takes a machine out of rotation before a deploy.",
    "---",
    "",
    "# Rollout",
    "",
    line,
    "",
  ].join("\n");
}

async function openWorkspace(page: Page): Promise<void> {
  const name = unique("Skills");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
}

/**
 * Picks a value from an Ant Design select, by option title.
 *
 * By the option's `title` rather than by its role: rc-select renders a second,
 * screen-reader-only option list carrying the same role, and a role query
 * finds that one first.
 */
async function choose(page: Page, label: string, value: string): Promise<void> {
  await page.getByLabel(label).click();
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator(`.ant-select-item-option[title="${value}"]`)
    .click();
}

async function bindTool(page: Page, name: string): Promise<void> {
  await unfold(page, "能力");
  const box = page.getByRole("checkbox", { name });
  await expect(box).not.toBeChecked();
  await page.locator(".ant-checkbox-wrapper").filter({ hasText: name }).click();
  await expect(box).toBeChecked();
}

/** Uploads one SKILL.md through the picker. No archive is ever built. */
async function uploadSkill(page: Page, line: string): Promise<void> {
  const skills = await openSection(page, "工具与技能", "技能", "skills");
  await skills.getByLabel("选择文件").setInputFiles({
    name: "SKILL.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(skillDocument(line), "utf-8"),
  });
  await expect(skills.getByRole("heading", { name: SKILL_NAME })).toBeVisible();
}

/** Publishes an Agent that binds the skill and the platform tools it needs. */
async function publishAgent(page: Page, scenario: string, tool: string): Promise<string> {
  const name = unique(scenario);
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await page.getByRole("button", { name: "新建 Agent" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByLabel("别名").fill(name.toLowerCase().replace(/_/g, "-"));
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page).toHaveURL(/\/agents\/[0-9a-f-]{36}$/);

  // Wait for the draft to land before typing into the form: its values arrive
  // as the form's initial values, and anything typed before they do is
  // replaced by them.
  await expect(page.getByText("草稿修订 1")).toBeVisible();
  await page.getByLabel("人格").fill(`A ${scenario} agent for the skills acceptance walk.`);
  await choose(page, "模型场景", scenario);
  await bindTool(page, tool);
  // Bound by version: the option reads "rollout v1" and what is stored is that
  // version's id.
  await choose(page, "技能", `${SKILL_NAME} v1`);
  // Read the form back before saving it. A select whose click was swallowed
  // leaves its default in place and this walk publishes it — which is how a
  // wrong scenario once travelled three steps before showing up as a missing
  // event on a timeline.
  await expect(page.getByText(scenario, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(`${SKILL_NAME} v1`, { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿修订 2")).toBeVisible();
  await page.getByRole("button", { name: "发布" }).click();
  await page.getByRole("button", { name: "确定" }).click();
  await expect(page.getByText("当前版本 v1")).toBeVisible();
  return name;
}

async function submitRun(page: Page, agentName: string): Promise<void> {
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("button", { name: "提交任务" }).click();
  await choose(page, "Agent", agentName);
  // The drill's whole vocabulary: the input names the skill to act on.
  await page.getByLabel("输入").fill(SKILL_NAME);
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
}

function timeline(page: Page) {
  return page.locator(".ant-card", { hasText: "时间线" }).first();
}

test("upload a skill, bind it, load it in a Run, propose a change, approve it", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await openWorkspace(page);
  await uploadSkill(page, "Take the machine out of the pool first, then drain it.");

  // -- the skill reaches a Run ------------------------------------------
  const reader = await publishAgent(page, "skill_once", "skill.load");
  await submitRun(page, reader);
  await expect(page.getByText("completed", { exact: true }).first()).toBeVisible({
    timeout: 90_000,
  });
  await expect(timeline(page).getByText("skill_loaded")).toBeVisible();
  // The sentence, not just the event name: it says which document entered the
  // conversation and that the document is a workspace's material. Matched on
  // the prose rather than on `SKILL.md`, which also appears in the raw payload
  // this entry carries beside it.
  await expect(
    timeline(page).getByText(/模型加载了技能 rollout 的 SKILL\.md/),
  ).toBeVisible();
  await expect(timeline(page).getByText(/技能正文是参考资料/)).toBeVisible();

  // -- the Agent suggests a change ---------------------------------------
  const author = await publishAgent(page, "propose_once", "skill.propose");
  await submitRun(page, author);
  await expect(page.getByText("completed", { exact: true }).first()).toBeVisible({
    timeout: 90_000,
  });
  await expect(timeline(page).getByText("skill_proposed")).toBeVisible();

  // -- a person reads the difference and decides --------------------------
  const proposals = await openSection(page, "待办", "技能提案", "proposals");
  await expect(proposals.getByText("Agent 提出")).toBeVisible();
  await proposals.getByRole("button", { name: "差异" }).click();
  await expect(page.getByText(/Check the dashboard before you start\./)).toBeVisible();
  await page.getByRole("button", { name: "批准并发布新版本" }).click();
  await expect(page.getByText(/已发布版本 2/)).toBeVisible();

  // -- and the Agent that proposed it did not change ----------------------
  const published = await openSection(page, "工具与技能", "技能", "skills");
  await expect(published.getByText("版本 2")).toBeVisible();
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await page.getByRole("link", { name: author }).click();
  // Still v1 of the Agent, and its draft still names the skill version it was
  // published against. §15.3's last sentence, seen from the console: approving
  // a proposal publishes a skill version and moves nothing.
  await expect(page.getByText("当前版本 v1")).toBeVisible();
  // The binding lives in 「能力」, folded on a fresh load; the fold's own bar
  // says how many skills are bound, not which version.
  await unfold(page, "能力");
  await expect(page.getByText(`${SKILL_NAME} v1`)).toBeVisible();
});
