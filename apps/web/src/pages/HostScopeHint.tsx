import { useQuery } from "@tanstack/react-query";
import { Alert } from "antd";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { OutboundScopeEntry } from "../api/types";
import { useT } from "../i18n/locale";
import { fill } from "../runs/explain";

/**
 * Whether an approved entry covers a host, as `outbound/domain/scope.py`'s
 * `matches_host` decides it: one spelling of the name, and a wildcard that
 * covers what is beneath a domain but not the domain itself. Network entries
 * are left out — a name is not inside a network until it is resolved.
 */
export function covers(entries: readonly string[], host: string): boolean {
  const candidate = clean(host);
  return entries.some((raw) => {
    const entry = clean(raw);
    if (entry.includes("/")) return false;
    if (!entry.startsWith("*.")) return entry === candidate;
    const suffix = entry.slice(1);
    return candidate.endsWith(suffix) && candidate !== suffix.slice(1);
  });
}

function clean(name: string): string {
  return name.trim().toLowerCase().replace(/\.+$/, "");
}

function hostOf(url: string): string | null {
  try {
    const parsed = new URL(url.trim());
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    // An address literal is matched against networks, which need the
    // resolution this page does not do — so it gets no verdict at all.
    if (/^[\d.]+$/.test(parsed.hostname) || parsed.hostname.startsWith("[")) return null;
    return parsed.hostname;
  } catch {
    return null;
  }
}

/**
 * Says, while the base address is typed, which outbound level has not
 * approved its host.
 *
 * A hint, not the check: registration asks the workspace list on the server,
 * and the egress proxy asks both lists at call time. The two levels fail at
 * different moments, which is why both are named — a host only the workspace
 * approved registers fine and is then refused on every call. A list this
 * person cannot read gives no verdict rather than a wrong one.
 */
export function HostScopeHint({ workspaceId, baseUrl }: { workspaceId: string; baseUrl: string | undefined }) {
  const t = useT();
  const host = baseUrl === undefined ? null : hostOf(baseUrl);
  const scope = { workspace: workspaceId };
  const platform = useQuery({
    queryKey: ["outbound-scopes", "platform"] as const,
    queryFn: () => api<OutboundScopeEntry[]>("/api/v1/outbound-scopes/platform", scope),
    enabled: host !== null,
  });
  const workspace = useQuery({
    queryKey: ["outbound-scopes", "workspace", workspaceId] as const,
    queryFn: () => api<OutboundScopeEntry[]>("/api/v1/outbound-scopes/workspace", scope),
    enabled: host !== null,
  });
  if (host === null || platform.data === undefined || workspace.data === undefined) return null;

  const inWorkspace = covers(workspace.data.map((entry) => entry.entry), host);
  const inPlatform = covers(platform.data.map((entry) => entry.entry), host);
  if (inWorkspace && inPlatform) {
    return <Alert className="host-scope-hint" type="success" showIcon title={fill(t("hostInScope"), { host })} />;
  }
  const toPlatform = (
    <Link target="_blank" rel="noopener noreferrer" to={`/workspaces/${workspaceId}/platform#outbound`}>
      {t("hostApprovePlatform")}
    </Link>
  );
  const toWorkspace = (
    <Link target="_blank" rel="noopener noreferrer" to={`/workspaces/${workspaceId}/settings#outbound`}>
      {t("hostApproveWorkspace")}
    </Link>
  );
  // Neither is not two independent gaps: a workspace entry the platform does
  // not cover is refused, so the platform has to go first.
  const said = !inWorkspace && !inPlatform
    ? <>{fill(t("hostNotInEither"), { host })} {toPlatform} · {toWorkspace}</>
    : inWorkspace
      ? <>{fill(t("hostNotInPlatform"), { host })} {toPlatform}</>
      : <>{fill(t("hostNotInWorkspace"), { host })} {toWorkspace}</>;
  return <Alert className="host-scope-hint" type="warning" showIcon title={said} />;
}
