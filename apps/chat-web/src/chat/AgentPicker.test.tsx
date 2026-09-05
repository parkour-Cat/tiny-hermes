import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AgentPicker } from "./AgentPicker";
import { LocaleProvider } from "../i18n/locale";
import { t } from "../i18n/zh-CN";

const AGENTS = [
  { alias: "concierge", name: "客服 Concierge" },
  { alias: "weekly-report", name: "周报助手" },
];

test("the title is the Agent's name, never its alias", () => {
  render(<LocaleProvider><AgentPicker agents={AGENTS} alias="concierge" onAgent={() => undefined} /></LocaleProvider>);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("客服 Concierge");
  expect(screen.queryByText("concierge")).toBeNull();
});

test("one allowed Agent means a title and no menu", () => {
  render(<LocaleProvider><AgentPicker agents={[AGENTS[0]!]} alias="concierge" onAgent={() => undefined} /></LocaleProvider>);
  expect(screen.queryByRole("button")).toBeNull();
});

test("the menu offers exactly what the credential allows, and hands back the alias", async () => {
  const onAgent = vi.fn();
  render(<LocaleProvider><AgentPicker agents={AGENTS} alias="concierge" onAgent={onAgent} /></LocaleProvider>);

  await userEvent.click(screen.getByRole("button", { name: t("pickAgent") }));
  const options = screen.getAllByRole("option");
  expect(options.map((option) => option.textContent)).toEqual(["客服 Concierge", "周报助手"]);
  await userEvent.click(screen.getByRole("option", { name: "周报助手" }));

  expect(onAgent).toHaveBeenCalledWith("weekly-report");
});

test("an alias the list does not name falls back to itself", () => {
  // The list can lag a credential exchange by a moment; the title must not
  // go blank while it does.
  render(<LocaleProvider><AgentPicker agents={[]} alias="concierge" onAgent={() => undefined} /></LocaleProvider>);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("concierge");
});
