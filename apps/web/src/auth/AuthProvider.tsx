import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { api, ApiError } from "../api/client";

export type User = {
  id: string;
  subject: string;
  display_name: string;
  status: string;
  is_platform_admin: boolean;
};

type LoginInput = {
  subject: string;
  password: string;
};

type AuthValue = {
  user: User | null;
  loading: boolean;
  error: string | null;
  login: (input: LoginInput) => Promise<void>;
  logout: () => Promise<void>;
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
      setUser(await api<User>("/api/v1/auth/me"));
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
    const authenticated = await api<User>("/api/v1/auth/sessions", {
      method: "POST",
      body: JSON.stringify(input),
    });
    setUser(authenticated);
    setError(null);
  }

  async function logout(): Promise<void> {
    await api<void>("/api/v1/auth/sessions/current", { method: "DELETE" });
    setUser(null);
  }

  const value = useMemo(
    () => ({ user, loading, error, login, logout, refresh }),
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
