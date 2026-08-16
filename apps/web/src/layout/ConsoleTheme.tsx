import { ConfigProvider, theme } from "antd";
import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";

const DARK_QUERY = "(prefers-color-scheme: dark)";
const STORAGE_KEY = "tiny-hermes-theme";

export type ThemeChoice = "light" | "dark";

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
          token: {
            colorPrimary: "#155e75",
            borderRadius: 10,
            fontFamily: 'Inter, "Noto Sans SC", system-ui, sans-serif',
          },
        }}
      >
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
