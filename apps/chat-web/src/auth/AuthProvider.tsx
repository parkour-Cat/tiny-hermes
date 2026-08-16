import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { ApiError } from "../api/client";
import { consoleBackend } from "../backend/console";
import type { SignInInput, Viewer } from "../backend/types";

/**
 * Identity, without the console's own user document.
 *
 * The chat surface used to hold `User`, `is_platform_admin` and all — a
 * control-console fact that means nothing to somebody talking to an Agent,
 * and that a page serving product design §4.5's end user could not produce.
 * `Viewer` is the three fields this surface actually shows.
 */
export type User = Viewer;

type LoginInput = SignInInput;

type AuthValue = {
  user: Viewer | null;
  loading: boolean;
  error: string | null;
  login: (input: LoginInput) => Promise<void>;
  logout: () => Promise<void>;
  forget: () => void;
  refresh: () => Promise<void>;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Identity is not scoped to a workspace, so this backend has none.
  const backend = useMemo(() => consoleBackend(null), []);
  const [user, setUser] = useState<Viewer | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function refresh(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      setUser(await backend.viewer());
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setUser(null);
      } else {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function login(input: LoginInput): Promise<void> {
    setUser(await backend.signIn(input));
    setError(null);
  }

  async function logout(): Promise<void> {
    await backend.signOut();
    setUser(null);
  }

  /**
   * Drop the signed-in user without asking the platform to end anything.
   *
   * For the case where the platform has already ended it: a `401` on any
   * request means the browser is holding a session that no longer exists, and
   * calling `DELETE` on it would only produce a second `401`.
   */
  function forget(): void {
    setUser(null);
  }

  const value = useMemo(
    () => ({ user, loading, error, login, logout, forget, refresh }),
    [user, loading, error],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("AuthProvider is missing");
  }
  return value;
}
