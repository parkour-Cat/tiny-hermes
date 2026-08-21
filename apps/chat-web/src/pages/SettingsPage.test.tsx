import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { SettingsPage } from "./SettingsPage";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";
import { ChatTheme } from "../theme/ChatTheme";

function renderSettings(): void {
  render(
    <ChatTheme>
      <LocaleProvider>
        <MemoryRouter initialEntries={["/settings"]}>
          <SettingsPage />
        </MemoryRouter>
      </LocaleProvider>
    </ChatTheme>,
  );
}

test("the page has no account section — the platform was never given a name or email", () => {
  renderSettings();

  expect(screen.queryByText("账号")).toBeNull();
  expect(screen.queryByText("名称")).toBeNull();
  expect(screen.queryByText("默认智能体")).toBeNull();
});

test("export calls the self-service door and offers the response as a file", async () => {
  let requested = 0;
  server.use(
    http.get("/api/v1/end-user/subjects/me/export", () => {
      requested += 1;
      return HttpResponse.json({
        subject_type: "end_user",
        subject_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        workspace_id: "11111111-2222-4333-8444-555555555555",
        memories: [],
        sessions: [],
      });
    }),
  );
  const clicked: string[] = [];
  const originalClick = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) {
    clicked.push(this.download);
  };
  try {
    renderSettings();
    await userEvent.click(screen.getByRole("button", { name: "导出" }));
    await waitFor(() => expect(requested).toBe(1));
    await waitFor(() => expect(clicked).toEqual(["tiny-hermes-my-data.json"]));
  } finally {
    HTMLAnchorElement.prototype.click = originalClick;
  }
});

test("erase asks for confirmation before calling the self-service door", async () => {
  let requested = 0;
  server.use(
    http.post("/api/v1/end-user/subjects/me/erase", () => {
      requested += 1;
      return HttpResponse.json({ memories: 2, sessions: 1, messages: 5, artifacts: 0 });
    }),
  );
  renderSettings();

  await userEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(requested).toBe(0);
  expect(screen.getByText(/确定要删除吗/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "删除" }));

  await waitFor(() => expect(requested).toBe(1));
  expect(await screen.findByText("已删除。")).toBeInTheDocument();
});
