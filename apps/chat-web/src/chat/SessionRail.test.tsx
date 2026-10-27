import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { SessionRail } from "./SessionRail";
import { emptySessionPrefs } from "./sessionPrefs";
import { LocaleProvider } from "../i18n/locale";
import { ChatTheme } from "../theme/ChatTheme";

const sessions = [
  { id: "s1", title: "周报", createdAt: new Date().toISOString() },
  { id: "s2", title: "订单", createdAt: new Date().toISOString() },
];

function Rail({ active, onSession }: { active: string; onSession: (id: string) => void }) {
  return (
    <SessionRail
      sessions={sessions}
      activeSessionId={active}
      prefs={emptySessionPrefs()}
      onPrefs={() => undefined}
      onSession={onSession}
      onNewChat={() => undefined}
      creating={false}
    />
  );
}

test("on a phone the rail folds behind one button, and closes once a conversation is chosen", async () => {
  // Stacked open above the conversation, the rail pushed the composer off a
  // phone's screen. Folding is CSS on a narrow screen; what can be checked
  // here is the switch and that choosing closes it.
  const wrap = (active: string) => (
    <ChatTheme>
      <MemoryRouter>
        <LocaleProvider>
          <Rail active={active} onSession={(id) => view.rerender(wrap(id))} />
        </LocaleProvider>
      </MemoryRouter>
    </ChatTheme>
  );
  const view = render(wrap("s1"));
  const toggle = screen.getByRole("button", { name: "会话" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(document.getElementById(toggle.getAttribute("aria-controls")!)).toContainElement(
    screen.getByRole("navigation", { name: "会话" }),
  );
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await userEvent.click(screen.getByText("订单"));
  expect(screen.getByRole("button", { name: "会话" })).toHaveAttribute("aria-expanded", "false");
});
