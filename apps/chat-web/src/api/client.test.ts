import { http, HttpResponse } from "msw";
import { expect, test } from "vitest";

import { api, ApiError } from "./client";
import { server } from "../test/server";

test("a validation refusal's list of problems does not become the message", async () => {
  // FastAPI answers a body it cannot read with `detail` as a list. Read as a
  // sentence it rendered as "[object Object]".
  server.use(
    http.post("/api/v1/thing", () =>
      HttpResponse.json({ detail: [{ loc: ["body", "text"], msg: "Field required", type: "missing" }] }, { status: 422 }),
    ),
  );

  const error = await api("/api/v1/thing", { method: "POST" }).catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(ApiError);
  expect((error as ApiError).code).toBe("request_failed");
  expect((error as ApiError).message).toBe("");
});

test("a refusal with no sentence leaves the wording to whoever shows it", async () => {
  // The transport has no locale: a Chinese sentence chosen here is the one the
  // language switch could not move.
  server.use(http.get("/api/v1/thing", () => new HttpResponse("oops", { status: 500 })));

  const error = await api("/api/v1/thing").catch((caught: unknown) => caught);
  expect((error as ApiError).message).toBe("");
});
