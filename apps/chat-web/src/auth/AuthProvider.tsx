import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { exchangeEndUserSession } from "../api/session";

type AuthValue = {
  /** Exchanging the URL's credential for a session cookie, right now. */
  loading: boolean;
  /** The exchange itself failed — a bad or expired credential, an unknown
   * or disabled issuer, an Agent this end user was not assigned. Nothing
   * past this state is reachable without the host page reopening the chat
   * with a fresh credential (design §4.5.1: the platform cannot re-issue
   * one on its own). */
  error: string | null;
  /** A request made after a successful exchange came back unauthenticated —
   * the 8-hour session ended, or an admin revoked it (design §4.3). Kept
   * apart from `error`: this app was working and then stopped, which reads
   * differently than "never connected". */
  lost: boolean;
  forget: () => void;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lost, setLost] = useState(false);

  useEffect(() => {
    // The credential lives in the URL fragment (`#credential=...`), never
    // the query string: a fragment is part of the URL the browser keeps to
    // itself — it is never sent in the HTTP request line, so nginx's
    // access log (or any proxy in between) has nothing to record. `?
    // workspace=&agent=` stay in the query because neither is a secret an
    // enterprise-signed 15-minute bearer credential is.
    const fragment = window.location.hash;
    const credential = fragment.startsWith("#")
      ? new URLSearchParams(fragment.slice(1)).get("credential")
      : null;
    const workspace = params.get("workspace");
    const agent = params.get("agent");
    if (credential === null || workspace === null || agent === null) {
      // No credential in this load's URL: nothing to exchange. The cookie
      // from an earlier exchange, if any, is what every request from here
      // on trusts — there is no "am I still signed in" call to make first
      // (design has no such endpoint; a session's validity is proven by
      // using it, not by asking about it in advance).
      setLoading(false);
      return;
    }
    void exchangeEndUserSession(credential, workspace)
      .then(() => {
        // The credential is a bearer secret with a 15-minute ceiling
        // (design §4.1) and must not sit in the URL bar or browser history
        // a moment longer than the one request that needed it.
        navigate(`/${agent}`, { replace: true });
        setLoading(false);
      })
      .catch((caught: unknown) => {
        setError(caught instanceof Error ? caught.message : String(caught));
        setLoading(false);
      });
    // Intentionally runs once: the credential this load's URL carried is
    // spent after the first attempt, successful or not, and re-running on
    // param or navigate identity changes would replay a used-up token.
  }, []);

  function forget(): void {
    setLost(true);
  }

  const value = useMemo(() => ({ loading, error, lost, forget }), [loading, error, lost]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("AuthProvider is missing");
  }
  return value;
}
