import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";

afterEach(() => vi.restoreAllMocks());

test("logs in and creates a workspace through the API", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  fetchMock
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ code: "unauthenticated" }), { status: 401 }),
    )
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          id: "u1",
          subject: "admin@example.com",
          display_name: "Admin",
          status: "active",
          is_platform_admin: true,
        }),
        { status: 201 },
      ),
    )
    .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ id: "w1", name: "Acme", status: "active" }), {
        status: 201,
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
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/v1/workspaces",
    expect.objectContaining({ method: "POST" }),
  );
});
