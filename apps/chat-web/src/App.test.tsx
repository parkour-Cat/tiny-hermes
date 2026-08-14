import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, test } from "vitest";

import { App } from "./App";
import { server } from "./test/server";

const USER = {
  id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  subject: "dev@example.com",
  display_name: "Dev",
  status: "active",
  is_platform_admin: false,
};
const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";

test("sign-in opens a conversation surface", async () => {
  let signedIn = false;
  server.use(
    http.get("/api/v1/auth/me", () =>
      signedIn
        ? HttpResponse.json(USER)
        : HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
    ),
    http.post("/api/v1/auth/sessions", () => {
      signedIn = true;
      return HttpResponse.json(USER, { status: 201 });
    }),
    http.get("/api/v1/workspaces", () =>
      HttpResponse.json([{ id: WORKSPACE, name: "Acme", status: "active" }]),
    ),
    http.get("/api/v1/agents", () =>
      HttpResponse.json([
        {
          id: AGENT,
          name: "Darwin",
          alias: "darwin",
          status: "published",
          current_version_id: "v1",
          created_at: "2026-08-10T00:00:00Z",
        },
      ]),
    ),
    http.get(`/api/v1/agents/${AGENT}`, () =>
      HttpResponse.json({
        id: AGENT,
        name: "Darwin",
        alias: "darwin",
        status: "published",
        current_version_id: "v1",
        created_at: "2026-08-10T00:00:00Z",
      }),
    ),
    http.get("/api/v1/sessions", () => HttpResponse.json([])),
  );

  render(<App />);
  await userEvent.type(await screen.findByLabelText("邮箱"), "dev@example.com");
  await userEvent.type(screen.getByLabelText("密码"), "long-pass-123");
  await userEvent.click(screen.getByRole("button", { name: "进入对话" }));

  expect(await screen.findByRole("heading", { name: "Darwin" })).toBeInTheDocument();
  expect(screen.getByLabelText("写给智能体")).toBeInTheDocument();
  expect(screen.queryByText("新建工作空间")).toBeNull();
  expect(screen.queryByText("试验场")).toBeNull();
});

test("a session the platform has already ended returns the user to sign-in", async () => {
  window.history.pushState({}, "", `/${WORKSPACE}/${AGENT}`);
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/workspaces", () =>
      HttpResponse.json([{ id: WORKSPACE, name: "Acme", status: "active" }]),
    ),
    http.get("/api/v1/agents", () =>
      HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
    ),
    http.get(`/api/v1/agents/${AGENT}`, () =>
      HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
    ),
    http.get("/api/v1/sessions", () =>
      HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
    ),
  );

  render(<App />);

  expect(await screen.findByRole("button", { name: "进入对话" })).toBeInTheDocument();
});
