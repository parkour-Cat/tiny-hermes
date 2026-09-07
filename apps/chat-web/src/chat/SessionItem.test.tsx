import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { LocaleProvider } from "../i18n/locale";
import { SessionItem } from "./SessionItem";

test("session removal confirmation keeps keyboard focus and cancellation returns to the action", async () => {
  render(<LocaleProvider><SessionItem session={{ id: "session", title: "测试对话" }}
    active pinned={false} archived={false} onOpen={() => undefined} onPin={() => undefined}
    onArchive={() => undefined} onForget={() => undefined} /></LocaleProvider>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "会话操作" }));
  await user.tab();
  await user.tab();
  expect(screen.getByRole("button", { name: "从列表移除" })).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(screen.getByRole("button", { name: "从列表移除" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.getByRole("button", { name: "会话操作" })).toHaveFocus();
});
