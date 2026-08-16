import { useMemo } from "react";

import { consoleBackend } from "./console";
import type { ChatBackend } from "./types";

/**
 * The backend this conversation talks to.
 *
 * One construction point. Before this existed, `X-Workspace-Id`, the CSRF
 * cookie and `/api/v1` paths were spelled out in thirteen files — twenty-nine
 * times in `ChatPage` alone — so serving product design §4.5's end user, who
 * has no workspace at all, would have meant editing every one of them.
 *
 * What this seam does **not** yet do, deliberately: the URL still carries a
 * workspace, and "which Agents may I talk to" is still answered by walking
 * the console's workspace list. Both of those follow from decisions nobody
 * has made yet — how an end user proves who they are, and what "已分配
 * Agent" is a unit of — and a seam that guessed them would be worse than one
 * that stops here. See
 * `docs/superpowers/research/2026-08-16-end-user-entry.md` §4.
 */
export function useBackend(workspaceId: string | null): ChatBackend {
  return useMemo(() => consoleBackend(workspaceId), [workspaceId]);
}
