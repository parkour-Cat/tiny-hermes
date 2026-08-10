import { ConfigProvider, theme } from "antd";
import type { ReactNode } from "react";
import { useSyncExternalStore } from "react";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/**
 * Whether the operating system asks for a dark surface.
 *
 * There is no in-app toggle and nothing persisted: a preference the console
 * stores is a preference it can disagree with the system about, and phase 4
 * owns that decision. Following the system is the one answer that cannot drift.
 */
export function useDarkPreference(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(DARK_QUERY).matches,
    () => false,
  );
}

/** The single place the console's Ant Design theme is decided. */
export function ConsoleTheme({ children }: { children: ReactNode }) {
  const dark = useDarkPreference();
  return (
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
  );
}
