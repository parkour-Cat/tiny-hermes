import { expect, test } from "@playwright/test";
import type { APIRequestContext } from "@playwright/test";

/**
 * The stream's query-parameter form, which the console no longer uses.
 *
 * The console reads events with `fetch`, so it sends `X-Workspace-Id`-free
 * requests only by way of the query parameter design §9.1 added for
 * `EventSource` clients — and it sets `Last-Event-ID` as a header. That leaves
 * the documented `EventSource` shape with no consumer at all, and an interface
 * kept for a client nobody tests is an interface that quietly stops working.
 * This subscribes the way an `EventSource` would: session cookie, workspace in
 * the query string, no request headers of its own.
 */

const RUN_UNTIL_TERMINAL = 120_000;

/** The CSRF token the browser session was issued, read from its own cookies. */
async function csrfToken(request: APIRequestContext): Promise<string> {
  const { cookies } = await request.storageState();
  const cookie = cookies.find((entry) => entry.name === "tiny_hermes_csrf");
  expect(cookie, "the signed-in state carries a CSRF cookie").toBeDefined();
  return decodeURIComponent(cookie?.value ?? "");
}

test("an EventSource-shaped subscription is served, and an unscoped one is refused", async ({
  request,
}) => {
  const csrf = await csrfToken(request);
  const write = { "X-CSRF-Token": csrf };
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1_000)}`;

  const workspace = await request.post("/api/v1/workspaces", {
    headers: write,
    data: { name: `Stream-${suffix}` },
  });
  expect(workspace.status()).toBe(201);
  const workspaceId = (await workspace.json()).id as string;
  const scoped = { ...write, "X-Workspace-Id": workspaceId };

  const agent = await request.post("/api/v1/agents", {
    headers: scoped,
    data: { name: `Stream-${suffix}`, alias: `stream-${suffix}` },
  });
  expect(agent.status()).toBe(201);
  const agentId = (await agent.json()).id as string;

  const draft = await request.put(`/api/v1/agents/${agentId}/draft`, {
    headers: scoped,
    data: {
      expected_revision: 1,
      spec: {
        schema_version: 1,
        personality: "An agent that completes in one round.",
        model_policy: { provider: "deterministic", scenario: "complete" },
        tools: [],
        limits: {
          max_execution_seconds: 900,
          max_elapsed_seconds: 86_400,
          max_model_calls: 20,
          max_tool_calls: 50,
          max_derived_retries: 3,
        },
      },
    },
  });
  expect(draft.status()).toBe(200);

  const published = await request.post(`/api/v1/agents/${agentId}/publish`, {
    headers: scoped,
    data: { expected_revision: 2 },
  });
  expect(published.status()).toBe(201);

  const session = await request.post("/api/v1/sessions", {
    headers: scoped,
    data: { agent_id: agentId, session_mode: "persistent" },
  });
  expect(session.status()).toBe(201);

  const run = await request.post("/api/v1/runs", {
    headers: { ...scoped, "Idempotency-Key": `stream-${suffix}` },
    data: { session_id: (await session.json()).id, input: "Subscribe to me." },
  });
  expect(run.status()).toBe(201);
  const runId = (await run.json()).id as string;

  // No workspace anywhere: not in the query string, not in a header. The stream
  // says so in its own words rather than answering with an empty subscription.
  const unscoped = await request.get(`/api/v1/runs/${runId}/events`);
  expect(unscoped.status()).toBe(400);
  expect((await unscoped.json()).code).toBe("workspace_required");

  // The `EventSource` shape. The body ends when the Run does, so reading it
  // whole is a subscription that ran to the end of the Run.
  const stream = await request.get(
    `/api/v1/runs/${runId}/events?workspace_id=${workspaceId}`,
    { timeout: RUN_UNTIL_TERMINAL },
  );
  expect(stream.status()).toBe(200);
  expect(stream.headers()["content-type"]).toContain("text/event-stream");

  const body = await stream.text();
  const frames = body
    .split("\n\n")
    .filter((block) => block.includes("data:"))
    .map((block) => {
      const line = block.split("\n").find((part) => part.startsWith("data:")) as string;
      return JSON.parse(line.slice("data:".length)) as { sequence: number; event_type: string };
    });

  expect(frames.length).toBeGreaterThan(1);
  expect(frames.map((entry) => entry.sequence)).toEqual(
    Array.from({ length: frames.length }, (_, index) => index + 1),
  );
  expect(frames.map((entry) => entry.event_type)).toContain("run_completed");
  // The framing an EventSource actually parses, not just JSON that arrived.
  expect(body).toContain(`id: ${frames[0]?.sequence}`);
  expect(body).toContain(`event: ${frames[0]?.event_type}`);
});
