import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { useWorkspaceId } from "./useWorkspaceId";

export type Role = "workspace_admin" | "developer" | "viewer" | "platform_admin";

/** 当前成员在当前工作空间里的角色，用来决定**不画**哪些段。
 *
 * 拿不到答案时是 `null`，不是某个默认角色：猜一个的后果是画出一个这个人点不动
 * 的段，而少画一段最多是少一个入口。`platform_admin` 是不是成员的平台管理员
 * 得到的答案——不是 workspace 角色，但控制台需要知道这个人能看见一切。 */
export function useMyRole(): { role: Role | null; loading: boolean } {
  const workspaceId = useWorkspaceId();
  const query = useQuery({
    queryKey: ["my-role", workspaceId],
    queryFn: () => api<{ role: Role }>(`/api/v1/workspaces/${workspaceId}/members/me`),
    enabled: workspaceId !== null,
    retry: false,
  });
  return { role: query.data?.role ?? null, loading: query.isLoading };
}
