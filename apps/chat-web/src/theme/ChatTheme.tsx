import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";

const DARK_QUERY = "(prefers-color-scheme: dark)";
const STORAGE_KEY = "tiny-hermes-chat-theme";

export type ThemeChoice = "light" | "dark";

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function useDarkPreference(): boolean {
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
  setTheme: (choice: ThemeChoice) => void;
  toggle: () => void;
};

const ThemeContext = createContext<ThemeValue | null>(null);

export function useChatTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (value === null) {
    throw new Error("ChatTheme is missing");
  }
  return value;
}

export function ChatTheme({ children }: { children: ReactNode }) {
  const systemDark = useDarkPreference();
  const [stored, setStored] = useState<ThemeChoice | null>(readStoredTheme);
  const dark = stored === null ? systemDark : stored === "dark";
  const value = useMemo<ThemeValue>(
    () => {
      const setTheme = (choice: ThemeChoice) => {
        setStored(choice);
        try {
          window.localStorage.setItem(STORAGE_KEY, choice);
        } catch {
          // A blocked store still lets this session switch.
        }
      };
      return {
        dark,
        setTheme,
        toggle: () => setTheme(dark ? "light" : "dark"),
      };
    },
    [dark],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }, [dark]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
