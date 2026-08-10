import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, test } from "vitest";

import { App } from "./App";
import { server } from "./test/server";

const ADMIN = {
  id: "u1",
  subject: "admin@example.com",
  display_name: "Admin",
  status: "active",
  is_platform_admin: true,
};

test("logs in and creates a workspace through the API", async () => {
  let signedIn = false;
  const workspaces: { id: string; name: string; status: string }[] = [];
  const created: string[] = [];
  server.use(
    http.get("/api/v1/auth/me", () =>
      signedIn ? HttpResponse.json(ADMIN) : HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
    ),
    http.post("/api/v1/auth/sessions", () => {
      signedIn = true;
      return HttpResponse.json(ADMIN, { status: 201 });
    }),
    http.get("/api/v1/workspaces", () => HttpResponse.json(workspaces)),
    http.post("/api/v1/workspaces", async ({ request }) => {
      const body = (await request.json()) as { name: string };
      created.push(body.name);
      const workspace = { id: "w1", name: body.name, status: "active" };
      workspaces.push(workspace);
      return HttpResponse.json(workspace, { status: 201 });
    }),
  );

  render(<App />);
  await userEvent.type(await screen.findByLabelText("邮箱"), "admin@example.com");
  await userEvent.type(screen.getByLabelText("密码"), "long-pass-123");
  await userEvent.click(screen.getByRole("button", { name: "登录" }));
  await userEvent.click(await screen.findByRole("button", { name: "新建工作空间" }));
  await userEvent.type(screen.getByLabelText("名称"), "Acme");
  await userEvent.click(screen.getByRole("button", { name: "创建" }));

  expect(await screen.findByText("Acme")).toBeInTheDocument();
  expect(created).toEqual(["Acme"]);
});
