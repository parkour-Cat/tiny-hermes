/**
 * Routing for the end-user surface: `/:alias/:sessionRef?`.
 *
 * No workspace segment. Design §7's embed carries one workspace and one
 * Agent alias per load (the host page's own choice, passed once at
 * exchange time) — there is no discovery endpoint an end user could use to
 * browse a second one, so a route built to disambiguate several workspaces
 * sharing an alias (the console client's own `paths.ts`) has nothing left
 * to disambiguate here.
 */

//: The alias grammar `agents/domain/models.py` enforces on the platform
//: side — lowercase words joined by hyphens. Reused here only to keep a
//: route param that is obviously not one from looking silly as a Session id.
const ALIAS = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;

export function isAgentAlias(value: string | undefined): value is string {
  return value !== undefined && ALIAS.test(value);
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function chatPath(alias: string, sessionId?: string | null): string {
  return sessionId === undefined || sessionId === null
    ? `/${alias}`
    : `/${alias}/${shortId(sessionId)}`;
}

/** The full Session id a short route ref names, or the ref itself if it
 * already is one — a bookmarked short link and a freshly created Session's
 * own id both have to resolve. */
export function matchSessionId(ids: string[], ref: string | null): string | null {
  if (ref === null) {
    return null;
  }
  const exact = ids.find((id) => id === ref);
  if (exact !== undefined) {
    return exact;
  }
  const prefixed = ids.filter((id) => id.startsWith(ref));
  return prefixed[0] ?? null;
}
