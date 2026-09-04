import { expect, test } from "vitest";

import { NAV_GROUPS, visibleSections } from "./navigation";

test("导航上恰好七个入口", () => {
  expect(NAV_GROUPS).toHaveLength(7);
});

test("十八个原有页面一个都没丢", () => {
  const paths = NAV_GROUPS.flatMap((g) => g.sections.map((s) => s.path));
  for (const path of [
    "agents", "runs", "channels", "approvals", "skill-proposals", "memory",
    "skills", "http-tools", "mcp-servers", "audit", "usage", "subjects",
    "members", "api-keys", "identity-providers", "model-endpoints", "secrets", "outbound",
  ]) {
    expect(paths, `${path} 不见了`).toContain(path);
  }
});

test("每个入口都有一句说明", () => {
  // 导航上只有词、没有说明，正是这次重组要解决的那件事。
  for (const group of NAV_GROUPS) expect(group.introKey).toBeTruthy();
});

test("viewer 看不到他会被拒绝的段，而 developer 看得到渠道", () => {
  // 依据是 2026-09-04 以两个角色实测各列表接口的结果，不是猜的。
  const settings = NAV_GROUPS.find((g) => g.key === "settings")!;
  expect(visibleSections(settings, "viewer", false).map((s) => s.key)).toEqual([
    "members",
    "model-endpoints",
    "outbound",
  ]);
  const channels = NAV_GROUPS.find((g) => g.key === "channels")!;
  expect(visibleSections(channels, "developer", false)).toHaveLength(1);
  expect(visibleSections(channels, "viewer", false)).toHaveLength(0);
});

test("身份提供方只跟着平台管理员的标志走，不跟着角色走", () => {
  const settings = NAV_GROUPS.find((g) => g.key === "settings")!;
  const keysFor = (role: Parameters<typeof visibleSections>[1], flag: boolean) =>
    visibleSections(settings, role, flag).map((s) => s.key);
  expect(keysFor("workspace_admin", false)).not.toContain("identity-providers");
  expect(keysFor("workspace_admin", true)).toContain("identity-providers");
  // 不是成员的平台管理员看得见其余一切。
  expect(keysFor("platform_admin", true)).toHaveLength(settings.sections.length);
});
