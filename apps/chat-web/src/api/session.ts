import { api } from "./client";

/**
 * The auth seam `apps/chat-web` currently authenticates through: a
 * workspace member's own session (`/api/v1/auth/*`), the same identity the
 * console uses.
 *
 * Collected behind this one module on purpose (design §7's plan, and the
 * research note behind it): swapping this app's identity for an end user's
 * own session should mean rewriting what is inside these three functions,
 * not hunting down every page that happens to call `api("/api/v1/auth/...")`
 * directly. This commit only moves the three requests out of `AuthProvider`
 * — nothing about what they send, or when, has changed.
 */

export type User = {
  id: string;
  subject: string;
  display_name: string;
  status: string;
  is_platform_admin: boolean;
};

export type LoginInput = {
  subject: string;
  password: string;
};

export function fetchCurrentUser(): Promise<User> {
  return api<User>("/api/v1/auth/me");
}

export function signIn(input: LoginInput): Promise<User> {
  return api<User>("/api/v1/auth/sessions", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function signOut(): Promise<void> {
  return api<void>("/api/v1/auth/sessions/current", { method: "DELETE" });
}
