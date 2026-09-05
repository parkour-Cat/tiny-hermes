/**
 * 旧地址到新入口的跳转表。**长期保留**：一个能打开的链接不会因为新导航上线就
 * 变得不该打开。锚点让它落在对应的段上，而不只是那一页的顶部。
 *
 * 一个表而不是十五条 `<Route>` 的原因：路由和它的测试要读同一份事实。
 */
export const LEGACY_REDIRECTS: readonly (readonly [from: string, to: string, anchor: string])[] = [
  ["approvals", "inbox", "approvals"],
  ["skill-proposals", "inbox", "proposals"],
  ["memory", "inbox", "memory"],
  ["skills", "tooling", "skills"],
  ["http-tools", "tooling", "http-tools"],
  ["mcp-servers", "tooling", "mcp-servers"],
  ["audit", "records", "audit"],
  ["usage", "records", "usage"],
  ["subjects", "records", "subjects"],
  ["members", "settings", "members"],
  ["api-keys", "settings", "api-keys"],
  ["identity-providers", "settings", "identity-providers"],
  ["model-endpoints", "settings", "model-endpoints"],
  ["secrets", "settings", "secrets"],
  ["outbound", "settings", "outbound"],
];
