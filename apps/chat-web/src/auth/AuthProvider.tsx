import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { ApiError } from "../api/client";
import { fetchCurrentUser, signIn as requestSignIn, signOut as requestSignOut } from "../api/session";
import type { LoginInput, User } from "../api/session";

export type { User };

type AuthValue = {
  user: User | null;
  loading: boolean;
  error: string | null;
  login: (input: LoginInput) => Promise<void>;
  logout: () => Promise<void>;
  forget: () => void;
  refresh: () => Promise<void>;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function refresh(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      setUser(await fetchCurrentUser());
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
    const authenticated = await requestSignIn(input);
    setUser(authenticated);
    setError(null);
  }

  async function logout(): Promise<void> {
    await requestSignOut();
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
