import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { enUS } from "./en-US";
import type { MessageKey } from "./zh-CN";
import { zhCN } from "./zh-CN";

export type Locale = "zh-CN" | "en-US";

const STORAGE_KEY = "tiny-hermes-chat-locale";
const CATALOGS: Record<Locale, Record<MessageKey, string>> = {
  "zh-CN": zhCN,
  "en-US": enUS,
};

type LocaleValue = {
  locale: Locale;
  t: (key: MessageKey) => string;
  setLocale: (locale: Locale) => void;
};

const LocaleContext = createContext<LocaleValue | null>(null);

function readStoredLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "zh-CN" || stored === "en-US") {
      return stored;
    }
  } catch {
    // A blocked store must not take chat down.
  }
  return "zh-CN";
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readStoredLocale);
  const value = useMemo<LocaleValue>(
    () => ({
      locale,
      t: (key) => CATALOGS[locale][key],
      setLocale: (next) => {
        setLocaleState(next);
        try {
          window.localStorage.setItem(STORAGE_KEY, next);
        } catch {
          // Persistence is best-effort.
        }
      },
    }),
    [locale],
  );
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale(): LocaleValue {
  const value = useContext(LocaleContext);
  if (value === null) {
    throw new Error("LocaleProvider is missing");
  }
  return value;
}

export function useT(): (key: MessageKey) => string {
  return useLocale().t;
}
