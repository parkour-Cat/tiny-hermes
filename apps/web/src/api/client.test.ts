import { http, HttpResponse } from "msw";
import { afterEach, expect, test } from "vitest";

import { api, ApiError } from "./client";
import { server } from "../test/server";

const CSRF_COOKIE = "tiny_hermes_csrf=token-value";

function withCsrfCookie(): void {
  document.cookie = CSRF_COOKIE;
}

afterEach(() => {
  document.cookie = "tiny_hermes_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

/** Echoes back the headers a request actually carried. */
function echoHeaders(path: string) {
  return http.all(path, ({ request }) =>
    HttpResponse.json({
      csrf: request.headers.get("X-CSRF-Token"),
      workspace: request.headers.get("X-Workspace-Id"),
    }),
  );
}

type Echo = { csrf: string | null; workspace: string | null };

test("a read carries no csrf token", async () => {
  withCsrfCookie();
  server.use(echoHeaders("/api/v1/thing"));

  expect((await api<Echo>("/api/v1/thing")).csrf).toBeNull();
});

// PUT is the regression this file exists for: the phase-1 helper allowlisted
// POST, PATCH, and DELETE, so saving an agent draft would have failed with
// csrf_failed on the first attempt.
test.each(["POST", "PUT", "PATCH", "DELETE"])("a %s carries the csrf token", async (method) => {
  withCsrfCookie();
  server.use(echoHeaders("/api/v1/thing"));

  const echo = await api<Echo>("/api/v1/thing", { method });

  expect(echo.csrf).toBe("token-value");
});

test("a scoped request carries the workspace it was given", async () => {
  server.use(echoHeaders("/api/v1/agents"));

  const echo = await api<Echo>("/api/v1/agents", { workspace: "w-1" });

  expect(echo.workspace).toBe("w-1");
});

test("an unscoped request carries no workspace header at all", async () => {
  server.use(echoHeaders("/api/v1/workspaces"));

  expect((await api<Echo>("/api/v1/workspaces")).workspace).toBeNull();
});

test("problem details context survives onto the error", async () => {
  server.use(
    http.get("/api/v1/runs/r-1/events", () =>
      HttpResponse.json(
        {
          code: "event_cursor_too_old",
          detail: "Re-read the run snapshot before resubscribing to its events.",
          context: { earliest_available_sequence: 12, run_url: "/api/v1/runs/r-1" },
        },
        { status: 410 },
      ),
    ),
  );

  const caught = await api("/api/v1/runs/r-1/events").catch((error: unknown) => error);

  expect(caught).toBeInstanceOf(ApiError);
  const error = caught as ApiError;
  expect(error.status).toBe(410);
  expect(error.code).toBe("event_cursor_too_old");
  // Without this the console cannot resynchronize, and the timeline silently
  // truncates instead of showing a gap.
  expect(error.context.earliest_available_sequence).toBe(12);
});

test("a no-content response resolves to undefined", async () => {
  server.use(http.delete("/api/v1/auth/sessions/current", () => new HttpResponse(null, { status: 204 })));

  await expect(api("/api/v1/auth/sessions/current", { method: "DELETE" })).resolves.toBeUndefined();
});

test("an unreachable platform is a network failure, not a request failure", async () => {
  server.use(http.get("/api/v1/thing", () => HttpResponse.error()));

  const caught = (await api("/api/v1/thing").catch((error: unknown) => error)) as ApiError;

  expect(caught.status).toBe(0);
  expect(caught.code).toBe("network_failed");
});
