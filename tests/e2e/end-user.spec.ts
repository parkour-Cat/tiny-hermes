import { generateKeyPairSync, sign as cryptoSign } from "node:crypto";
import { readFile } from "node:fs/promises";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * Design §7's own walk: an enterprise signs a credential, opens the chat
 * surface with it, has a conversation, closes and reopens the same address
 * and finds the same conversation, then uses the subject's own self-service
 * door to export what the platform holds about them.
 *
 * Setup — registering the issuer, publishing an end-user-enabled Agent —
 * runs through the console's own signed-in session, the same `api()`
 * pattern `memory.spec.ts` and `skills.spec.ts` use: the console's builder
 * has no UI for `end_user_access` or `channel_issuers` yet, so driving it
 * through the API is what actually exercises the routes rather than a form
 * that does not exist. The conversation itself runs in a fresh browser
 * context with no console cookie in it at all — design §3's own point is
 * that these are two identity systems, and a walk that reused the signed-in
 * page for the end-user half would not be proving they stay apart.
 */

const CHAT_ORIGIN = process.env.TINY_HERMES_E2E_CHAT_URL ?? "http://127.0.0.1:3001";
const ISSUER = "https://idp.acme.example";

function unique(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1_000)}`;
}

async function openWorkspace(page: Page): Promise<string> {
  const name = unique("EndUser");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]{36}\/agents$/);
  const url = new URL(page.url());
  return url.pathname.split("/")[2] as string;
}

/** The console API, with the signed-in browser's own session and CSRF
 * token — the same helper `memory.spec.ts` defines for itself. */
async function api(
  page: Page,
  workspaceId: string,
  method: string,
  path: string,
  body?: unknown,
): Promise<{ status: number; json: any }> {
  return page.evaluate(
    async ({ method, path, body, workspaceId }) => {
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
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await response.text();
      return { status: response.status, json: text ? JSON.parse(text) : null };
    },
    { method, path, body, workspaceId },
  );
}

/** Publishes an Agent with `end_user_access.enabled: true` — the platform-
 * side gate design §5 requires on top of the credential's own `agents`
 * claim (the enterprise-side half) before an end user can reach it at all. */
async function publishEndUserAgent(
  page: Page,
  workspaceId: string,
): Promise<{ agentId: string; alias: string }> {
  const name = unique("concierge");
  const alias = name.toLowerCase().replace(/_/g, "-");
  const created = await api(page, workspaceId, "POST", "/api/v1/agents", { name, alias });
  expect(created.status).toBe(201);
  const agentId = created.json.id as string;
  const draft = await api(page, workspaceId, "PUT", `/api/v1/agents/${agentId}/draft`, {
    expected_revision: 1,
    spec: {
      schema_version: 1,
      personality: "A concierge available to an enterprise's own end users.",
      model_policy: { provider: "deterministic", scenario: "complete" },
      tools: [],
      limits: {
        max_execution_seconds: 900,
        max_elapsed_seconds: 86_400,
        max_model_calls: 20,
        max_tool_calls: 50,
        max_derived_retries: 3,
      },
      end_user_access: { enabled: true },
    },
  });
  expect(draft.status).toBe(200);
  const published = await api(page, workspaceId, "POST", `/api/v1/agents/${agentId}/publish`, {
    expected_revision: draft.json.revision,
  });
  expect(published.status).toBe(201);
  return { agentId, alias };
}

function rsaKeyPair(): { publicKey: string; privateKey: string } {
  return generateKeyPairSync("rsa", {
    modulusLength: 2048,
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
  });
}

function base64url(input: Buffer): string {
  return input.toString("base64url");
}

/** A minimal RS256 JWT, signed with `node:crypto` rather than a library —
 * the platform's own decision doc (design plan §9) adds PyJWT to the
 * backend precisely because hand-rolled verification is where JWT bugs
 * live; signing one known-good token for a test carries none of that risk,
 * so it does not buy a new dependency into the workspace for it. */
function signCredential(claims: Record<string, unknown>, privateKey: string): string {
  const header = base64url(Buffer.from(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const payload = base64url(Buffer.from(JSON.stringify(claims)));
  const signingInput = `${header}.${payload}`;
  const signature = cryptoSign("RSA-SHA256", Buffer.from(signingInput), privateKey);
  return `${signingInput}.${base64url(signature)}`;
}

async function registerIssuer(
  page: Page,
  workspaceId: string,
  publicKey: string,
): Promise<void> {
  const registered = await api(page, workspaceId, "POST", "/api/v1/channel-issuers", {
    channel: "web",
    issuer: ISSUER,
    public_key: publicKey,
    allowed_origins: [CHAT_ORIGIN],
  });
  expect(registered.status).toBe(201);
}

/**
 * Plan §10's own walk: a Run that stops for the end user's own confirmation,
 * seen and answered from `apps/chat-web` rather than read out of the
 * database the way `test_end_user_approvals.py` (necessarily) does — this
 * is the assertion that suite's own docstring says the previous stage could
 * not make.
 *
 * The stand-in is the platform's own API, the same choice `tools.spec.ts`
 * makes and for the same reason: `GET /health/live` needs no credential and
 * a real egress hop out through the proxy is what is under test, not the far
 * end. `TOOL_HOST = "api"` is the compose service name, reachable from the
 * Worker container once both outbound-scope levels approve it.
 */
const TOOL_HOST = "api";
const TOOL_BASE = `http://${TOOL_HOST}:8000`;
const TOOL_DOCUMENT = JSON.stringify({
  openapi: "3.0.3",
  info: { title: "Platform health", version: "1" },
  paths: {
    "/health/live": {
      get: { operationId: "readLiveness", summary: "Is the API alive." },
      post: { operationId: "pokeLiveness", summary: "Pretend to change it." },
    },
  },
});

async function approveEgressHost(page: Page, workspaceId: string, entry: string): Promise<void> {
  const platform = await api(page, workspaceId, "POST", "/api/v1/outbound-scopes/platform", {
    entry,
    note: "plan §10's confirmation walk",
  });
  expect(platform.status).toBe(201);
  const workspace = await api(page, workspaceId, "POST", "/api/v1/outbound-scopes/workspace", {
    entry,
    note: "plan §10's confirmation walk",
  });
  expect(workspace.status).toBe(201);
}

async function registerHttpTool(page: Page, workspaceId: string): Promise<string> {
  const created = await api(page, workspaceId, "POST", "/api/v1/http-tools", {
    name: "health",
    base_url: TOOL_BASE,
    document: TOOL_DOCUMENT,
    credential_ref: null,
  });
  expect(created.status).toBe(201);
  const versions = await api(
    page,
    workspaceId,
    "GET",
    `/api/v1/http-tools/${created.json.id}/versions`,
  );
  expect(versions.status).toBe(200);
  return versions.json[0].id as string;
}

/**
 * An end-user-enabled Agent whose one bound write is `write_policy:
 * "governance"` — on a `caller_type=end_user` Run that opens a
 * `user_confirmation` rather than a `governance_approval` (plan §5's
 * producer), which is the state plan §10's routes and this walk exist for.
 */
async function publishConfirmableAgent(
  page: Page,
  workspaceId: string,
  httpToolVersionId: string,
): Promise<{ agentId: string; alias: string }> {
  const name = unique("writer");
  const alias = name.toLowerCase().replace(/_/g, "-");
  const created = await api(page, workspaceId, "POST", "/api/v1/agents", { name, alias });
  expect(created.status).toBe(201);
  const agentId = created.json.id as string;
  const draft = await api(page, workspaceId, "PUT", `/api/v1/agents/${agentId}/draft`, {
    expected_revision: 1,
    spec: {
      schema_version: 1,
      personality: "A concierge that writes on the enterprise's own behalf.",
      model_policy: { provider: "deterministic", scenario: "http_once" },
      tools: [],
      network: { allow: [TOOL_HOST] },
      http_tools: [
        {
          http_tool_version_id: httpToolVersionId,
          operations: ["pokeLiveness"],
          write_policy: "governance",
        },
      ],
      limits: {
        max_execution_seconds: 900,
        max_elapsed_seconds: 86_400,
        max_model_calls: 20,
        max_tool_calls: 50,
        max_derived_retries: 3,
      },
      end_user_access: { enabled: true },
    },
  });
  expect(draft.status).toBe(200);
  const published = await api(page, workspaceId, "POST", `/api/v1/agents/${agentId}/publish`, {
    expected_revision: draft.json.revision,
  });
  expect(published.status).toBe(201);
  return { agentId, alias };
}

test("an enterprise credential opens a conversation that survives closing the tab, and the end user exports their own data", async ({
  page,
  browser,
}) => {
  const workspaceId = await openWorkspace(page);
  const { alias } = await publishEndUserAgent(page, workspaceId);
  const { publicKey, privateKey } = rsaKeyPair();
  await registerIssuer(page, workspaceId, publicKey);

  const now = Math.floor(Date.now() / 1000);
  const credential = signCredential(
    {
      iss: ISSUER,
      sub: "e2e-end-user",
      aud: workspaceId,
      iat: now,
      exp: now + 600,
      agents: [alias],
    },
    privateKey,
  );

  // A fresh browser context: design §3's own point is that an end user's
  // identity is not a platform member's, so this walk starts with no
  // console cookie anywhere in it, the same as a real visitor would.
  const context = await browser.newContext();
  let chatPage = await context.newPage();
  // The credential rides the URL fragment, not the query string (task-7
  // review finding 1): a fragment is never sent in the HTTP request, so
  // apps/chat-web/nginx.conf's access log — which does record the query
  // string — never sees it. workspace/agent stay in the query; neither is
  // a secret.
  const url = `${CHAT_ORIGIN}/?workspace=${workspaceId}&agent=${alias}#credential=${encodeURIComponent(credential)}`;
  await chatPage.goto(url);

  // The credential exchanged and the app landed on the clean, alias-only
  // address — the URL a bookmark or a page reload will use from here on.
  await expect(chatPage).toHaveURL(new RegExp(`^${CHAT_ORIGIN}/${alias}$`));
  await expect(chatPage.getByLabel("写给智能体")).toBeVisible();

  await chatPage.getByLabel("写给智能体").fill("Hello from the enterprise's own page.");
  await chatPage.getByRole("button", { name: "发送" }).click();

  await expect(chatPage.getByText("Hello from the enterprise's own page.")).toBeVisible();
  // The deterministic model's own "complete" scenario replies promptly; the
  // frontend polls GET .../runs/{id} rather than subscribing to a stream
  // (a reduction from the console's SSE, noted in this task's report), so
  // this waits on the polled outcome rather than an event.
  await expect
    .poll(async () => chatPage.locator(".turn-agent").count(), { timeout: 60_000 })
    .toBeGreaterThan(0);

  const conversationUrl = chatPage.url();

  // Close the tab and open a new one in the same context — the same cookie
  // jar a real browser keeps across closing and reopening a window. No
  // credential this time: the whole claim is that the cookie alone is
  // enough to find the same person.
  await chatPage.close();
  chatPage = await context.newPage();
  await chatPage.goto(conversationUrl);

  await expect(chatPage.getByText("Hello from the enterprise's own page.")).toBeVisible();
  await expect(chatPage.locator(".turn-agent").first()).toBeVisible();

  // Design §4.6's self-service door: the subject's own export, off the
  // console entirely.
  await chatPage.goto(`${CHAT_ORIGIN}/settings`);
  const [download] = await Promise.all([
    chatPage.waitForEvent("download"),
    chatPage.getByRole("button", { name: "导出" }).click(),
  ]);
  const path = await download.path();
  expect(path).not.toBeNull();
  const exported = JSON.parse(await readFile(path as string, "utf-8"));
  expect(exported.subject_type).toBe("end_user");
  expect(exported.workspace_id).toBe(workspaceId);
  expect(Array.isArray(exported.sessions)).toBe(true);
  expect(exported.sessions.length).toBeGreaterThan(0);

  await context.close();
});

test("an end user hits a Run that stops for their own confirmation, sees it, and answers it", async ({
  page,
  browser,
}) => {
  test.setTimeout(180_000);
  const workspaceId = await openWorkspace(page);
  await approveEgressHost(page, workspaceId, TOOL_HOST);
  const httpToolVersionId = await registerHttpTool(page, workspaceId);
  const { alias } = await publishConfirmableAgent(page, workspaceId, httpToolVersionId);
  const { publicKey, privateKey } = rsaKeyPair();
  await registerIssuer(page, workspaceId, publicKey);

  const now = Math.floor(Date.now() / 1000);
  const credential = signCredential(
    {
      iss: ISSUER,
      sub: "e2e-confirming-user",
      aud: workspaceId,
      iat: now,
      exp: now + 600,
      agents: [alias],
    },
    privateKey,
  );

  // A fresh context, same reasoning as the walk above: this end user's
  // identity has never touched the console's own session.
  const context = await browser.newContext();
  const chatPage = await context.newPage();
  const url = `${CHAT_ORIGIN}/?workspace=${workspaceId}&agent=${alias}#credential=${encodeURIComponent(credential)}`;
  await chatPage.goto(url);
  await expect(chatPage).toHaveURL(new RegExp(`^${CHAT_ORIGIN}/${alias}$`));

  // `http.health.pokeLiveness` is the deterministic model's own drill
  // vocabulary (`_tool_call_name` in `deterministic_model.py`): the Run
  // input names exactly the bound write operation to call.
  await chatPage.getByLabel("写给智能体").fill("http.health.pokeLiveness");
  await chatPage.getByRole("button", { name: "发送" }).click();

  // The write stopped, and this end user's own page — not the console, not
  // a database query — is where that becomes visible. Before this task
  // there was no route that could produce this text at all.
  await expect(chatPage.getByText("这次运行需要你确认才能继续")).toBeVisible({
    timeout: 120_000,
  });
  // Scoped to the banner, not the page: the Run input *is* the operation
  // name, so it is also sitting in this person's own message bubble a few
  // lines up. An unscoped match finds both and proves neither — what is
  // worth asserting is that the banner itself says which write is waiting,
  // since that is the only thing telling them what they are agreeing to.
  await expect(
    chatPage.locator(".approval-banner .approval-tool"),
  ).toHaveText("http.health.pokeLiveness");

  await chatPage.getByRole("button", { name: "同意", exact: true }).click();

  // The confirmation cleared and the Run went on to do the write it had
  // stopped for — the half of the walk `test_end_user_approvals.py` proves
  // over HTTP but no browser-driven test could show until this task.
  await expect(chatPage.getByText("这次运行需要你确认才能继续")).toBeHidden({
    timeout: 30_000,
  });
  await expect
    .poll(async () => chatPage.locator(".turn-agent").count(), { timeout: 60_000 })
    .toBeGreaterThan(0);

  await context.close();
});

// Task-9 review finding B: `apps/chat-web/nginx.conf` dropped
// `X-Frame-Options` deliberately — this surface is meant to be embedded in
// an enterprise's own page — but the comment defending that claimed the
// origin allowlist (design §7's `resolve_end_user_caller_for_write`) "stands
// in for the protection X-Frame-Options would otherwise give up". It does
// not: a request issued from *inside* an attacker's frame carries the chat
// app's own origin, which is on the allowlist, so the origin check is
// structurally blind to clickjacking. `Content-Security-Policy:
// frame-ancestors` is the actual replacement, deploy-time configured and
// defaulting to `'none'` when nothing is set — this test is what the served
// document actually carries against a real nginx, not a claim about what
// the config file says.
test("the served document carries a frame-ancestors CSP, closed by default", async ({
  request,
}) => {
  const response = await request.get(`${CHAT_ORIGIN}/`);
  expect(response.ok()).toBe(true);
  const csp = response.headers()["content-security-policy"];
  expect(csp).toBeDefined();
  expect(csp).toContain("frame-ancestors");
  expect(csp).toContain("'none'");
});
