import { useQueries } from "@tanstack/react-query";

import { api } from "../api/client";

/** 三个队列的路径，与 ApprovalsPage / SkillProposalsPage / MemoryPage 各自读的
 *  那一个接口相同（2026-09-04 grep 过）。 */
const QUEUES = ["/api/v1/approvals", "/api/v1/skill-proposals", "/api/v1/memories/pending"];

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
  const queries = useQueries({
    queries: QUEUES.map((path) => ({
      queryKey: ["inbox-count", path],
      queryFn: () => api<unknown[]>(path),
      retry: false,
    })),
  });
  if (queries.some((query) => query.data === undefined)) return null;
  return queries.reduce((total, query) => total + (query.data?.length ?? 0), 0);
}
