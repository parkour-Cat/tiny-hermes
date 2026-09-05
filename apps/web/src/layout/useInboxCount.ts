import { useQueries } from "@tanstack/react-query";

import { api } from "../api/client";
import type { ApprovalsPageResponse } from "../api/types";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/** 三个队列，各按它自己接口的形状数。第一次真机走查发现审批接口回的是
 *  `{items, has_more}` 而不是数组，技能提案接口不带 `status` 时连已决定的
 *  也回——两处都让数字要么算不出、要么算多。 */
const QUEUES: { path: string; count: (body: unknown) => number }[] = [
  // `has_more` 为 true 时这是个下界；队列长到那一步的那天，就是该加计数接口的那天。
  { path: "/api/v1/approvals", count: (body) => (body as ApprovalsPageResponse).items.length },
  { path: "/api/v1/skill-proposals?status=pending", count: (body) => (body as unknown[]).length },
  { path: "/api/v1/memories/pending", count: (body) => (body as unknown[]).length },
];

/** 三个队列一共有几件事等着人处理。
 *
 * 并发调三个现有接口相加，不新加计数接口：不增后端面，权限沿用三个接口各自
 * 已有的判定，而且这三份数据进了缓存之后点进「待办」是即时的。代价是三个队列
 * 都很长时这是三次全量请求——**队列长到让这件事变慢的那天，就是该加计数接口
 * 的那天**。
 *
 * 任何一个读不到就回 `null`，不回部分和：一个部分和看起来是个准确的数，而它
 * 不是。 */
export function useInboxCount(): number | null {
  const workspaceId = useWorkspaceId();
  const queries = useQueries({
    queries: QUEUES.map(({ path, count }) => ({
      queryKey: ["inbox-count", workspaceId, path],
      // Scoped: all three routes refuse without `X-Workspace-Id`, and the
      // first real walk found the badge never appearing because of it.
      queryFn: async () => count(await api<unknown>(path, { workspace: workspaceId ?? "" })),
      enabled: workspaceId !== null,
      retry: false,
    })),
  });
  if (queries.some((query) => query.data === undefined)) return null;
  return queries.reduce((total, query) => total + (query.data ?? 0), 0);
}
