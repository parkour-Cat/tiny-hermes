import type { AgentSpecDocument } from "../api/types";

type Change = { path: string; before: string; after: string };

function normalized(spec: AgentSpecDocument): unknown {
  return { ...spec, end_user_access: spec.end_user_access ?? { enabled: false },
    delivery: spec.delivery ?? { enabled: false, sync_timeout_seconds: 60 },
    skills: spec.skills ?? [], http_tools: spec.http_tools ?? [], mcp_tools: spec.mcp_tools ?? [],
    network: spec.network ?? { allow: [] } };
}

function flattened(value: unknown, prefix = "", result: Record<string, string> = {}): Record<string, string> {
  if (value !== null && typeof value === "object" && Object.keys(value).length > 0) {
    for (const [key, child] of Object.entries(value)) flattened(child, prefix ? `${prefix}.${key}` : key, result);
  } else result[prefix] = JSON.stringify(value);
  return result;
}

export function specDiff(before: AgentSpecDocument, after: AgentSpecDocument): Change[] {
  const left = flattened(normalized(before));
  const right = flattened(normalized(after));
  return [...new Set([...Object.keys(left), ...Object.keys(right)])].sort()
    .filter((path) => left[path] !== right[path])
    .map((path) => ({ path, before: left[path] ?? "—", after: right[path] ?? "—" }));
}
