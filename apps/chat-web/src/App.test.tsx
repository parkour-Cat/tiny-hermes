import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { expect, test } from "vitest";

import { App } from "./App";
import { server } from "./test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const ALIAS = "darwin";
const CREDENTIAL = "header.payload.signature";

test("a credential in the URL exchanges a session and opens the chat", async () => {
  const exchanges: { authorization: string | null; workspace: string | null }[] = [];
  server.use(
    http.post("/api/v1/end-user/sessions", ({ request }) => {
      exchanges.push({
        authorization: request.headers.get("Authorization"),
        workspace: request.headers.get("X-Workspace-Id"),
      });
      return HttpResponse.json(
        { end_user_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", expires_at: "2026-08-20T20:00:00Z" },
        { status: 201 },
      );
    }),
  );
  window.history.pushState(
    {},
    "",
    `/?credential=${CREDENTIAL}&workspace=${WORKSPACE}&agent=${ALIAS}`,
  );

  render(<App />);

  await waitFor(() => expect(exchanges).toHaveLength(1));
  expect(exchanges[0]).toEqual({ authorization: `Bearer ${CREDENTIAL}`, workspace: WORKSPACE });
  expect(await screen.findByLabelText("写给智能体")).toBeInTheDocument();
  // The credential is spent the moment it is exchanged and must not linger
  // in the address bar (design §4.1's 15-minute ceiling is not a reason to
  // also leave it sitting in browser history).
  expect(window.location.search).toBe("");
  expect(window.location.pathname).toBe(`/${ALIAS}`);
});

test("a refused credential explains itself instead of offering a form to retry in", async () => {
  server.use(
    http.post("/api/v1/end-user/sessions", () =>
      HttpResponse.json(
        { code: "end_user_credential_invalid", detail: "The credential could not be verified." },
        { status: 401 },
      ),
    ),
  );
  window.history.pushState(
    {},
    "",
    `/?credential=${CREDENTIAL}&workspace=${WORKSPACE}&agent=${ALIAS}`,
  );

  render(<App />);

  expect(await screen.findByText("The credential could not be verified.")).toBeInTheDocument();
  expect(screen.queryByLabelText("邮箱")).toBeNull();
  expect(screen.queryByLabelText("密码")).toBeNull();
});

test("no credential and no known address waits to be opened from the host page", async () => {
  window.history.pushState({}, "", "/");

  render(<App />);

  expect(await screen.findByText("等待接入")).toBeInTheDocument();
});
