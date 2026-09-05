import { ConfigProvider, theme } from "antd";
import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";

const DARK_QUERY = "(prefers-color-scheme: dark)";
const STORAGE_KEY = "tiny-hermes-theme";

export type ThemeChoice = "light" | "dark";

// 纸与铜。底色是暖纸色而不是白，强调色是铜而不是 Ant Design 的蓝：这是控制台
// 自己的身份，也是聊天页已经在用的那一套。两套各自完整，深色不是亮色的反相。
const INK = "#1a1612";
const PAPER = "#f6f1e8";
const PAPER_RAISED = "#fbf7f0";
const COPPER = "#c45c26";
const LINE = "#ddd4c6";
const INK_DARK = "#f3ece3";
const PAPER_DARK = "#161310";
const PAPER_RAISED_DARK = "#1e1a16";
const COPPER_DARK = "#e08a4f";
const LINE_DARK = "#3a332c";

const FONT = '"Noto Sans SC", "Source Sans 3", ui-sans-serif, system-ui, sans-serif';

/** The Ant Design tokens for one theme; exported so a test can ask what a
 *  surface resolved to. */
export function consoleDesignToken(dark: boolean) {
  return dark
    ? {
        colorPrimary: COPPER_DARK,
        colorLink: COPPER_DARK,
        colorInfo: COPPER_DARK,
        colorBgBase: PAPER_DARK,
        colorBgContainer: PAPER_RAISED_DARK,
        colorBgLayout: PAPER_DARK,
        colorText: INK_DARK,
        colorBorder: LINE_DARK,
        colorBorderSecondary: LINE_DARK,
        borderRadius: 8,
        fontFamily: FONT,
      }
    : {
        colorPrimary: COPPER,
        colorLink: COPPER,
        colorInfo: COPPER,
        colorBgBase: PAPER,
        colorBgContainer: PAPER_RAISED,
        colorBgLayout: PAPER,
        colorText: INK,
        colorBorder: LINE,
        colorBorderSecondary: LINE,
        borderRadius: 8,
        fontFamily: FONT,
      };
}

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/**
 * Whether the operating system asks for a dark surface.
 *
 * Used only until the operator picks a theme. After that, localStorage wins.
 */
export function useDarkPreference(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(DARK_QUERY).matches,
    () => false,
  );
}

function readStoredTheme(): ThemeChoice | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") {
      return stored;
    }
  } catch {
    return null;
  }
  return null;
}

type ThemeValue = {
  dark: boolean;
  toggle: () => void;
};

const ThemeContext = createContext<ThemeValue | null>(null);

export function useConsoleTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (value === null) {
    throw new Error("ConsoleTheme is missing");
  }
  return value;
}

/** The single place the console's Ant Design theme is decided. */
export function ConsoleTheme({ children }: { children: ReactNode }) {
  const systemDark = useDarkPreference();
  const [stored, setStored] = useState<ThemeChoice | null>(readStoredTheme);
  const dark = stored === null ? systemDark : stored === "dark";
  const value = useMemo<ThemeValue>(
    () => ({
      dark,
      toggle: () => {
        const next: ThemeChoice = dark ? "light" : "dark";
        setStored(next);
        try {
          window.localStorage.setItem(STORAGE_KEY, next);
        } catch {
          // Same as locale: a blocked store still lets this session switch.
        }
      },
    }),
    [dark],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }, [dark]);

  return (
    <ThemeContext.Provider value={value}>
      <ConfigProvider
        button={{ autoInsertSpace: false }}
        theme={{
          algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: consoleDesignToken(dark),
        }}
      >
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
