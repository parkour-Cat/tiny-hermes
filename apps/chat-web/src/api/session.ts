import { api } from "./client";

/**
 * The auth seam `apps/chat-web` authenticates through: an end user's own
 * session (design §4.2), never a workspace member's.
 *
 * This is the file the research note meant by "one file rather than
 * seventy" — everything above this module (`AuthProvider`, every page) asks
 * only "do I have an identity yet", never how one was obtained. Getting one
 * is exactly the one request below: an enterprise-signed credential,
 * exchanged once for the platform's own end-user session cookie
 * (`HttpOnly`, `SameSite=None`, `Secure`, set server-side — this module
 * never sees the cookie itself, only whether the exchange succeeded).
 */

export type EndUserSession = {
  end_user_id: string;
  expires_at: string;
};

export function exchangeEndUserSession(
  credential: string,
  workspaceId: string,
): Promise<EndUserSession> {
  return api<EndUserSession>("/api/v1/end-user/sessions", {
    method: "POST",
    workspace: workspaceId,
    headers: { Authorization: `Bearer ${credential}` },
  });
}
