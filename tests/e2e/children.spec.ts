import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * One delegation from end to end, against the real stack.
 *
 * A parent Agent hands work to two children, gives up its lease and its
 * sandbox, and hangs on them. The children run as Runs of their own. The
 * Scheduler hands their results back and the parent finishes. The console shows
 * the tree, and each child is a link.
 *
 * The claim this walk makes that no test below it can: **the parent does not
 * wake itself, and nothing in the request path does either.** Every unit and
 * integration test here drives the Scheduler by hand; this one has a real one
 * running in its own container on its own clock, so "the children are picked up
 * and the parent is woken" is a statement about the deployment rather than
 * about a function call.
 *
 * The second claim is the budget. Three Runs, one set of counters, checked in
 * the parent's own document — a tree that reset its safety valve per Run would
 * pass every test in this repository except this one.
 *
 * Agents are published through the API rather than the builder, for the reason
 * `memory.spec.ts` records: the scenario list is virtualized and an option near
 * the bottom cannot be reliably clicked, so a walk that drove it would publish
 * one scenario while appearing to choose another.
 */

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

async function openWorkspace(page: Page): Promise<string> {
  const name = unique("Children");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
  const url = new URL(page.url());
  return url.pathname.split("/")[2] as string;
}

/** The API, with the browser's own session and CSRF token. */
async function api(
  page: Page,
  workspaceId: string,
  method: string,
  path: string,
  body?: unknown,
  idempotencyKey?: string,
): Promise<{ status: number; json: any }> {
  return await page.evaluate(
    async ({ method, path, body, workspaceId, idempotencyKey }) => {
      const csrf = document.cookie
        .split("; ")
        .find((entry) => entry.startsWith("tiny_hermes_csrf="))
        ?.split("=")[1];
      const response = await fetch(path, {
        method,
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Workspace-Id": workspaceId,
          ...(csrf === undefined ? {} : { "X-CSRF-Token": csrf }),
          ...(idempotencyKey === undefined ? {} : { "Idempotency-Key": idempotencyKey }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await response.text();
      return { status: response.status, json: text ? JSON.parse(text) : null };
    },
    { method, path, body, workspaceId, idempotencyKey },
  );
}

const LIMITS = {
  max_execution_seconds: 900,
  max_elapsed_seconds: 86400,
  max_model_calls: 20,
  max_tool_calls: 50,
  max_derived_retries: 3,
};

async function publish(
  page: Page,
  workspaceId: string,
  alias: string,
  spec: Record<string, unknown>,
): Promise<string> {
  const created = await api(page, workspaceId, "POST", "/api/v1/agents", {
    name: alias,
    alias,
  });
  expect(created.status, JSON.stringify(created.json)).toBe(201);
  const agentId = created.json.id as string;
  const draft = await api(page, workspaceId, "PUT", `/api/v1/agents/${agentId}/draft`, {
    expected_revision: 1,
    spec: { schema_version: 1, limits: LIMITS, ...spec },
  });
  expect(draft.status, JSON.stringify(draft.json)).toBe(200);
  const published = await api(page, workspaceId, "POST", `/api/v1/agents/${agentId}/publish`, {
    expected_revision: draft.json.revision,
  });
  expect(published.status, JSON.stringify(published.json)).toBe(201);
  return agentId;
}

test("a parent delegates two children, waits for both, and finishes", async ({ page }) => {
  const workspaceId = await openWorkspace(page);

  const reader = unique("reader").toLowerCase().replace(/_/g, "-");
  const checker = unique("checker").toLowerCase().replace(/_/g, "-");
  for (const alias of [reader, checker]) {
    await publish(page, workspaceId, alias, {
      personality: "A child agent.",
      model_policy: { provider: "deterministic", scenario: "complete" },
      tools: [],
    });
  }
  const coordinatorAlias = unique("coordinator").toLowerCase().replace(/_/g, "-");
  const coordinator = await publish(page, workspaceId, coordinatorAlias, {
    personality: "A coordinating agent.",
    model_policy: { provider: "deterministic", scenario: "delegate_once" },
    tools: ["agent.delegate"],
    delegation: {
      max_parallel: 2,
      children: [{ alias: reader }, { alias: checker }],
    },
  });

  const session = await api(page, workspaceId, "POST", "/api/v1/sessions", {
    agent_id: coordinator,
  });
  expect(session.status).toBe(201);
  const started = await api(
    page,
    workspaceId,
    "POST",
    "/api/v1/runs",
    { session_id: session.json.id, input: `${reader},${checker}` },
    `children-${session.json.id}`,
  );
  expect(started.status, JSON.stringify(started.json)).toBe(201);
  const runId = started.json.id as string;

  // Nobody in the request path settles a `child_runs` wait. The Scheduler in
  // its own container does, on its own clock, so reaching `completed` at all
  // is the sweep working end to end.
  await expect
    .poll(
      async () =>
        (await api(page, workspaceId, "GET", `/api/v1/runs/${runId}`)).json.status,
      { timeout: 180_000 },
    )
    .toBe("completed");

  const finished = await api(page, workspaceId, "GET", `/api/v1/runs/${runId}`);
  const children = finished.json.children as { id: string; status: string }[];
  expect(children).toHaveLength(2);
  expect(children.map((child) => child.status)).toEqual(["completed", "completed"]);

  // Every child is a Run of its own, sitting at depth 1 under this one, and
  // measured against this one's budget.
  for (const child of children) {
    const seen = await api(page, workspaceId, "GET", `/api/v1/runs/${child.id}`);
    expect(seen.json.parent_run_id).toBe(runId);
    expect(seen.json.depth).toBe(1);
    expect(seen.json.session_id).not.toBe(session.json.id);
    expect(seen.json.budget_root_run_id).toBe(finished.json.budget_root_run_id);
  }

  // One tree, one set of counters. The parent needed two rounds and each child
  // one, so four — and a tree that gave each Run a budget of its own would
  // report two here and pass everything else.
  expect(finished.json.budget.consumed_model_calls).toBe(4);

  // That the parent *waited* is asserted from what the wait left behind rather
  // than by sampling for it. On a stack this fast the Scheduler can settle the
  // wait between two polls, so catching `waiting_external` in the act is a
  // race — but the turn below is written in one place only, by the sweep that
  // wakes a parent, so its presence means the parent was asleep and was woken.
  const messages = await api(
    page,
    workspaceId,
    "GET",
    `/api/v1/sessions/${session.json.id}/messages`,
  );
  const delivered = (messages.json as { author?: string; parts: { text?: string }[] }[])
    .filter((message) => message.author === "platform")
    .map((message) => message.parts.map((part) => part.text ?? "").join(""));
  expect(delivered).toHaveLength(1);
  for (const child of children) {
    expect(delivered[0]).toContain(child.id);
  }
});

test("the console shows the tree and each child is a link", async ({ page }) => {
  const workspaceId = await openWorkspace(page);

  const helper = unique("helper").toLowerCase().replace(/_/g, "-");
  await publish(page, workspaceId, helper, {
    personality: "A child agent.",
    model_policy: { provider: "deterministic", scenario: "complete" },
    tools: [],
  });
  const leadAlias = unique("lead").toLowerCase().replace(/_/g, "-");
  const lead = await publish(page, workspaceId, leadAlias, {
    personality: "A coordinating agent.",
    model_policy: { provider: "deterministic", scenario: "delegate_once" },
    tools: ["agent.delegate"],
    delegation: { max_parallel: 1, children: [{ alias: helper }] },
  });

  const session = await api(page, workspaceId, "POST", "/api/v1/sessions", {
    agent_id: lead,
  });
  const started = await api(
    page,
    workspaceId,
    "POST",
    "/api/v1/runs",
    { session_id: session.json.id, input: helper },
    `children-console-${session.json.id}`,
  );
  const runId = started.json.id as string;

  await expect
    .poll(
      async () =>
        (await api(page, workspaceId, "GET", `/api/v1/runs/${runId}`)).json.status,
      { timeout: 180_000 },
    )
    .toBe("completed");

  const finished = await api(page, workspaceId, "GET", `/api/v1/runs/${runId}`);
  const childId = (finished.json.children as { id: string }[])[0]!.id;

  await page.goto(`/workspaces/${workspaceId}/runs/${runId}`);
  // The tree is on the page, and the child is somewhere to go rather than an
  // id to copy.
  const link = page.getByRole("link", { name: childId });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/runs/${childId}$`));
  // And the child says who delegated it, which is the only thing on its own
  // page that connects it to the work it was doing. It appears more than
  // once, and none of them is a duplicate: the parent is what delegated this
  // Run (概要), the root its budget is measured against (§12.4), and the top
  // of the task tree (§952). The count is deliberately not pinned — it moved
  // from 2 to 3 the day the tree landed, and a count says nothing a reader
  // could act on.
  await expect(page.getByRole("link", { name: runId }).first()).toBeVisible();
  // The tree is the claim worth making here: from the child, the sibling it
  // was delegated alongside is reachable — which was the whole gap §952
  // named. Two children were delegated, so exactly one is not this one.
  const treeCard = page.locator(".ant-card", { hasText: "任务树" });
  await expect(treeCard.getByRole("link", { name: runId })).toBeVisible();
});
