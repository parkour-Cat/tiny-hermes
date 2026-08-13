import { ConfigProvider } from "antd";
import type { ReactNode } from "react";

import { LocaleProvider } from "../i18n/locale";
import { ConsoleTheme } from "../layout/ConsoleTheme";

/**
 * The console's own theme, with animation switched off.
 *
 * jsdom parses CSS but never runs it, so a transition it is asked to start
 * never ends. Ant Design finishes closing a dialog — hiding the wrapper and
 * returning focus to whatever opened it — only once the leave transition
 * reports that it is done, so with animation left on, a dismissed dialog stays
 * in the document for the rest of the test and no assertion can tell a closed
 * dialog from an open one. Switched off, a dialog is either open or closed,
 * which is the only thing these tests ask about.
 *
 * Tests render this rather than `ConsoleTheme` directly: a test that skips the
 * theme asserts against a shell nobody ships.
 */
export function TestTheme({ children }: { children: ReactNode }) {
  return (
    <ConsoleTheme>
      <LocaleProvider>
        <ConfigProvider theme={{ token: { motion: false } }}>{children}</ConfigProvider>
      </LocaleProvider>
    </ConsoleTheme>
  );
}
