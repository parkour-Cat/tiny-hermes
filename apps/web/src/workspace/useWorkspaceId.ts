import { useParams } from "react-router-dom";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The one derivation from route parameter to `X-Workspace-Id`.
 *
 * Scope lives in the URL so a reload or a shared link reopens it, and so
 * reaching for another Workspace is an addressable action the server can refuse
 * rather than hidden client state. Every scoped request takes its workspace
 * from here; nothing else may decide one.
 *
 * Returns `null` for a parameter that cannot be a Workspace ID, so the caller
 * can refuse without sending a request the server would only reject.
 */
export function useWorkspaceId(): string | null {
  const { workspaceId } = useParams();
  return workspaceId !== undefined && UUID.test(workspaceId) ? workspaceId : null;
}
