import { createContext, useContext, type ReactNode } from "react";
import type { Role } from "./useMyRole";

const none = { writer: false, admin: false, platform: false };
const Context = createContext(none);

/** Presentation hints only; the backend continues to authorize every write. */
export function WorkspacePermissions({ role, platform = false, children }: { role: Role | null; platform?: boolean; children: ReactNode }) {
  const admin = platform || role === "platform_admin" || role === "workspace_admin";
  return <Context.Provider value={{ admin, writer: admin || role === "developer", platform: platform || role === "platform_admin" }}>{children}</Context.Provider>;
}

export const useWorkspacePermissions = () => useContext(Context);
