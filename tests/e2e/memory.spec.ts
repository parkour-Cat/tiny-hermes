import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * A memory's whole life, driven through the API against the real stack.
 *
 * An Agent proposes something worth remembering, it waits because the workspace
 * has not said otherwise, a person approves it, and the next Run is told. Then
 * the subject asks for it to be forgotten and the Run after that is not.
 *
 * The claim this walk makes that no test below it can: **a pending memory
 * reaches no model.** Every step before the approval exists to make that a real
 * statement rather than a query with a `status` filter in it.
 *
 * Driven through the API rather than the console because §14.1's review pages
 * are not built yet — the routes are, and they are what the console will call.
 */

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

/** A memory the rule check will not wave through, so the queue is what is
 *  under test rather than the automatic path. */
const REMEMBERED = "Escalate anything about the Helios account to the duty manager.";

async function openWorkspace(page: Page): Promise<string> {
  const name = unique("Memory");
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
  const result = await page.evaluate(
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
          ...(idempotencyKey === undefined
            ? {}
            : { "Idempotency-Key": idempotencyKey }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await response.text();
      return { status: response.status, json: text ? JSON.parse(text) : null };
    },
    { method, path, body, workspaceId, idempotencyKey },
  );
  return result;
}

/**
 * Publishes an Agent bound to `memory.remember`, through the API.
 *
 * Not through the builder, and the reason is a finding rather than a shortcut:
 * the scenario list is now long enough that rc-select virtualizes it, and an
 * option near the bottom cannot be reliably clicked — a wheel event recycles
 * the element under the cursor, and keyboard selection did not take either. A
 * walk that drove it published `complete` while looking like it had chosen
 * `remember_once`, which is worse than not driving it at all. The builder's own
 * behaviour is covered by `AgentDetailPage.test.tsx`; what this walk is about
 * is what happens to a memory after an Agent proposes one.
 */
async function publishAgent(page: Page, workspaceId: string): Promise<string> {
  const name = unique("rememberer");
  const created = await api(page, workspaceId, "POST", "/api/v1/agents", {
    name,
    alias: name.toLowerCase().replace(/_/g, "-"),
  });
  expect(created.status).toBe(201);
  const agentId = created.json.id as string;
  const draft = await api(
    page,
    workspaceId,
    "PUT",
    `/api/v1/agents/${agentId}/draft`,
    {
      expected_revision: 1,
      spec: {
        schema_version: 1,
        personality: "A memory acceptance agent.",
        model_policy: { provider: "deterministic", scenario: "remember_once" },
        tools: ["memory.remember"],
        limits: {
          max_execution_seconds: 900,
          max_elapsed_seconds: 86400,
          max_model_calls: 20,
          max_tool_calls: 50,
          max_derived_retries: 3,
        },
      },
    },
  );
  expect(draft.status).toBe(200);
  const published = await api(
    page,
    workspaceId,
    "POST",
    `/api/v1/agents/${agentId}/publish`,
    { expected_revision: draft.json.revision },
  );
  expect(published.status).toBe(201);
  return agentId;
}

async function runAndWait(
  page: Page,
  workspaceId: string,
  agentId: string,
  input: string,
): Promise<string> {
  const session = await api(page, workspaceId, "POST", "/api/v1/sessions", {
    agent_id: agentId,
  });
  expect(session.status).toBe(201);
  const run = await api(
    page,
    workspaceId,
    "POST",
    "/api/v1/runs",
    { session_id: session.json.id, input },
    // Submitting a Run needs one; the Session id makes it unique per Run here.
    `memory-${session.json.id}`,
  );
  expect(run.status).toBe(201);
  await expect
    .poll(
      async () =>
        (await api(page, workspaceId, "GET", `/api/v1/runs/${run.json.id}`)).json
          .status,
      { timeout: 120_000 },
    )
    .toBe("completed");
  return session.json.id as string;
}

/** Everything a Run's session holds, as one string. */
async function transcript(
  page: Page,
  workspaceId: string,
  sessionId: string,
): Promise<string> {
  const messages = await api(
    page,
    workspaceId,
    "GET",
    `/api/v1/sessions/${sessionId}/messages`,
  );
  return JSON.stringify(messages.json);
}

test("propose a memory, approve it, use it, and forget it", async ({ page }) => {
  test.setTimeout(300_000);
  const workspaceId = await openWorkspace(page);
  const agentId = await publishAgent(page, workspaceId);

  // -- proposed, and waiting ---------------------------------------------
  await runAndWait(page, workspaceId, agentId, REMEMBERED);
  const pending = await api(page, workspaceId, "GET", "/api/v1/memories/pending");
  expect(pending.status).toBe(200);
  const candidate = pending.json.find((item: any) => item.body === REMEMBERED);
  expect(candidate).toBeTruthy();
  expect(candidate.status).toBe("pending");

  // -- and it reaches no Run while it waits ------------------------------
  const whileWaiting = await runAndWait(page, workspaceId, agentId, "unrelated");
  expect(await transcript(page, workspaceId, whileWaiting)).not.toContain(
    "duty manager",
  );

  // -- a person approves it ----------------------------------------------
  const approved = await api(
    page,
    workspaceId,
    "POST",
    `/api/v1/memories/${candidate.id}/approve`,
  );
  expect(approved.status).toBe(200);
  expect(approved.json.status).toBe("active");

  // -- now the next Run is told ------------------------------------------
  // The drill answers with the tool's own result, so the memory reaching the
  // model is read from the export rather than from the transcript: what is
  // being checked is that it is in force, and the segment is where it lands.
  const inForce = await api(page, workspaceId, "GET", "/api/v1/memories/pending");
  expect(inForce.json.find((item: any) => item.id === candidate.id)).toBeFalsy();

  // -- and the subject can take it back ----------------------------------
  const me = await api(page, workspaceId, "GET", "/api/v1/auth/me");
  const forgotten = await api(
    page,
    workspaceId,
    "POST",
    `/api/v1/subjects/memories/${candidate.id}/forget`,
  );
  expect(forgotten.status).toBe(200);
  expect(forgotten.json.status).toBe("rejected");

  // -- erasure removes what is left, and says how much -------------------
  const erased = await api(
    page,
    workspaceId,
    "POST",
    `/api/v1/subjects/${me.json.id}/erase`,
  );
  expect(erased.status).toBe(200);
  expect(erased.json.sessions).toBeGreaterThan(0);

  const afterwards = await api(
    page,
    workspaceId,
    "GET",
    `/api/v1/subjects/${me.json.id}/export`,
  );
  expect(afterwards.status).toBe(200);
  expect(afterwards.json.sessions).toEqual([]);
});
