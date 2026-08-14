import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { LoginPage } from "./LoginPage";
import { AuthProvider } from "../auth/AuthProvider";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";
import { ChatTheme } from "../theme/ChatTheme";

test("the sign-in page is a conversation door, not the operator console", () => {
  server.use(
    http.get("/api/v1/auth/me", () =>
      HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
    ),
  );
  render(
    <ChatTheme>
      <LocaleProvider>
        <MemoryRouter>
          <AuthProvider>
            <LoginPage />
          </AuthProvider>
        </MemoryRouter>
      </LocaleProvider>
    </ChatTheme>,
  );

  expect(screen.getByRole("heading", { name: "开始对话" })).toBeInTheDocument();
  expect(screen.getByText(/不是管理后台/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "进入对话" })).toBeInTheDocument();
  expect(screen.queryByText("登录管理控制台")).toBeNull();
  expect(screen.queryByText("企业运行台")).toBeNull();
  expect(screen.queryByText("初始化平台")).toBeNull();
});
