# 控制台入口重组与表单收纳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把控制台的 18 个顶级入口收成 7 个，把三个最重的表单改成「折叠时显示当前值」的分段，并落实四条界面规则。

**Architecture:** 前端为主。新增一个后端只读接口回答「我在这个工作空间是什么角色」，因为前端现在拿不到它。合并出来的四个入口是同一个分段容器的四次使用，不是四份新代码。表单分段抽成一个组件，三页复用。

**Tech Stack:** React 18 + react-router + antd 5 + @tanstack/react-query + vitest/RTL（前端）；FastAPI + SQLAlchemy + pytest（后端）。

**事实来源：** `docs/superpowers/specs/2026-09-04-console-ia-and-forms-design.md`

## Global Constraints

- 顶级入口**恰好 7 个**：Agents、运行、渠道、待办、工具与技能、记录、设置。
- **一个入口都不许删。**18 个原有页面全部保留，只是换了位置。
- **旧地址全部保留成跳转**，且是长期行为，不是过渡措施。
- **前端隐藏不构成授权。**后端的拒绝仍是唯一权限判据；前端少画一段只是不邀请一次注定失败的点击。这句话必须以注释形式出现在实现隐藏逻辑的那个文件里。
- **一段只有在它此刻能通过校验时才允许折叠。** 新建时含未填必填项的段一律展开；任何时候校验失败，出错字段所在的段自动展开。
- **折叠条上必须显示这一段现在的值**，不能只显示段名。
- 角色取值只有三个：`workspace_admin`、`developer`、`viewer`；平台管理员不是角色，是 `is_platform_admin` 标志。
- 后端**只新增一个只读接口**，不改任何既有接口、数据或权限判定。
- 中文文案写进 `apps/web/src/i18n/zh-CN.ts`，英文写进 `en-US.ts`，两边键名一致。

---

## 文件结构

**新建**

| 文件 | 职责 |
| --- | --- |
| `apps/web/src/workspace/useMyRole.ts` | 一个 hook，回答「我在这个工作空间是什么角色」 |
| `apps/web/src/layout/navigation.ts` | 七个入口的定义：名字、说明、每段的路径与可见角色 |
| `apps/web/src/layout/GroupedPage.tsx` | 合并页的公共外壳：按角色过滤分段、渲染分段 |
| `apps/web/src/pages/InboxPage.tsx` | 待办（审批 · 提案 · 记忆审核） |
| `apps/web/src/pages/ToolingPage.tsx` | 工具与技能（技能 · HTTP 工具 · MCP 服务） |
| `apps/web/src/pages/RecordsPage.tsx` | 记录（审计 · 用量 · 主体数据） |
| `apps/web/src/pages/SettingsPage.tsx` | 设置（成员 · API 密钥 · 身份提供方 · 模型端点 · 密钥 · 出站范围） |
| `apps/web/src/forms/FormSection.tsx` | 可折叠分段：摘要行、必填展开、校验展开 |

**修改**

| 文件 | 改什么 |
| --- | --- |
| `packages/backend/src/tiny_hermes/tenancy/application/workspace_service.py` | 加 `my_role` |
| `packages/backend/src/tiny_hermes/tenancy/presentation/routes.py` | 加 `GET /{workspace_id}/members/me` |
| `apps/web/src/layout/ConsoleLayout.tsx` | 十八个 `NavLink` 换成七个 |
| `apps/web/src/App.tsx` | 新路由 + 旧地址跳转 |
| `apps/web/src/pages/ModelEndpointsPage.tsx` | 表单分段 |
| `apps/web/src/pages/AgentDetailPage.tsx` | 表单分段 |
| `apps/web/src/pages/ChannelsPage.tsx` | 表单去重 + 分段 + 表格三条界面规则 |

**现有的十八个页面组件一个都不动**（除上面点名的三个）。它们从「路由的目标」变成「分段的内容」，本身不变。

---

## Task 1: 后端交出「我是什么角色」

**Files:**
- Modify: `packages/backend/src/tiny_hermes/tenancy/application/workspace_service.py`
- Modify: `packages/backend/src/tiny_hermes/tenancy/presentation/routes.py`
- Test: `packages/backend/tests/integration/tenancy/test_workspace_routes.py`

**Interfaces:**
- Produces: `GET /api/v1/workspaces/{workspace_id}/members/me` → `{"role": "workspace_admin" | "developer" | "viewer" | "platform_admin"}`
- Produces: `WorkspaceService.my_role(actor: Actor, workspace_id: UUID) -> str`

- [ ] **Step 1: 写失败的测试**

加到 `packages/backend/tests/integration/tenancy/test_workspace_routes.py`。若该文件不存在，用同目录下已有的 workspace 路由测试文件；用 `ls packages/backend/tests/integration/tenancy/` 确认后放进去。

```python
async def test_a_viewer_can_ask_what_role_they_have(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """任何成员都可以问自己的角色——那不是别人的信息。

    这一条不能用 `/members` 代替：列成员是一个 viewer 可能被拒绝的动作，而
    「我是谁」不是。控制台需要这个答案来决定不画哪些段。
    """
    await _seed_user(engine, "Vera", "vera@example.com")
    viewer = _member(client, scope, workspace_id, "vera@example.com", "viewer")

    answered = client.get(f"/api/v1/workspaces/{workspace_id}/members/me", headers=viewer)

    assert answered.status_code == 200, answered.text
    assert answered.json() == {"role": "viewer"}


async def test_a_stranger_is_refused_their_own_role(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """不是成员就没有角色可报。回一个空角色会让前端把它当成「某种成员」。"""
    await _seed_user(engine, "Stan", "stan@example.com")
    stranger = _headers_for(client, "stan@example.com", workspace_id)

    refused = client.get(f"/api/v1/workspaces/{workspace_id}/members/me", headers=stranger)

    assert refused.status_code == 403, refused.text
```

`_seed_user`、`_member`、`_headers_for` 用该文件已有的同名夹具；`test_binding_routes.py` 里有 `_seed_user` 与 `_member` 的写法可以照抄。

- [ ] **Step 2: 跑它，看它红**

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"
uv run --no-sync pytest packages/backend/tests/integration/tenancy -k "role" -q
```
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 加服务方法**

在 `workspace_service.py` 的 `_require_member` 上方加：

```python
    async def my_role(self, actor: Actor, workspace_id: UUID) -> str:
        """这个人在这个工作空间里是什么角色。

        不走 `_require_member`：那一条问的是「能不能读这个工作空间」，而这里问
        的是「你是谁」。两者今天的答案恰好相同，但它们会各自演化——把「我是谁」
        建立在「我能读吗」之上，等于让一次权限收紧顺手改掉一个人的身份。

        平台管理员而非成员时回 `platform_admin`。它不是一个 workspace 角色，
        但控制台需要知道这个人能看见一切；伪装成 `workspace_admin` 会让页面
        显示一个他在这个工作空间里并不拥有的身份。
        """
        role = await self._store.get_membership(workspace_id, actor.id)
        if role is not None:
            return str(role)
        if actor.is_platform_admin:
            return "platform_admin"
        raise Forbidden
```

- [ ] **Step 4: 加路由**

在 `routes.py` 的 `list_members` 之后加。**必须放在 `/{workspace_id}/members/{user_id}` 之前**（若存在），否则 `me` 会被当成一个 user_id 匹配掉。

```python
    class MyRoleResponse(BaseModel):
        role: str

    @router.get("/{workspace_id}/members/me", response_model=MyRoleResponse)
    async def my_role(  # pyright: ignore[reportUnusedFunction]
        workspace_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency, scope="function")],
        selected_workspace: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> MyRoleResponse:
        user = await authenticate_browser_user(auth, session_token)
        _require_path_matches_header(workspace_id, selected_workspace)
        try:
            return MyRoleResponse(role=await workspaces.my_role(_actor(user), workspace_id))
        except Forbidden as error:
            raise forbidden() from error
```

`MyRoleResponse` 放在文件里其它 `BaseModel` 响应模型旁边，不要嵌在函数里。

- [ ] **Step 5: 跑测试，看它绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/tenancy -k "role" -q
uv run ruff check packages/backend && uv run pyright
```
Expected: 2 passed；ruff/pyright 干净

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/tenancy packages/backend/src/tiny_hermes/tenancy
git commit -m "feat(tenancy): 交出「我在这个工作空间是什么角色」"
```

---

## Task 2: 前端读到自己的角色

**Files:**
- Create: `apps/web/src/workspace/useMyRole.ts`
- Create: `apps/web/src/workspace/useMyRole.test.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/workspaces/{id}/members/me`（Task 1）
- Produces: `useMyRole(): { role: Role | null; loading: boolean }`，`type Role = "workspace_admin" | "developer" | "viewer" | "platform_admin"`

- [ ] **Step 1: 写失败的测试**

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";

import { useMyRole } from "./useMyRole";

const server = setupServer();
beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function wrap({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

test("reports the role the server gave", async () => {
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "viewer" })),
  );
  const { result } = renderHook(() => useMyRole(), { wrapper: wrap });
  await waitFor(() => expect(result.current.role).toBe("viewer"));
});

test("a refused answer is null, never a guessed role", async () => {
  // 猜一个角色的后果是画出一个这个人点不动的段。宁可少画。
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => new HttpResponse(null, { status: 403 })),
  );
  const { result } = renderHook(() => useMyRole(), { wrapper: wrap });
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.role).toBeNull();
});
```

被测文件所在目录若已有测试的 `wrap` 帮手，用它，不要再写一份；`ls apps/web/src/workspace/` 先看。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- useMyRole
```
Expected: FAIL，`Cannot find module './useMyRole'`

- [ ] **Step 3: 实现**

```ts
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { useWorkspaceId } from "./useWorkspaceId";

export type Role = "workspace_admin" | "developer" | "viewer" | "platform_admin";

/** 当前成员在当前工作空间里的角色，用来决定**不画**哪些段。
 *
 * 拿不到答案时是 `null`，不是某个默认角色：猜一个的后果是画出一个这个人点不动
 * 的段，而少画一段最多是少一个入口。 */
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
```

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test -- useMyRole
pnpm --filter @tiny-hermes/web exec tsc --noEmit
```
Expected: 2 passed；tsc 无输出

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/workspace/useMyRole.ts apps/web/src/workspace/useMyRole.test.tsx
git commit -m "feat(web): 读到当前成员在这个工作空间的角色"
```

---

## Task 3: 七个入口的定义表

**Files:**
- Create: `apps/web/src/layout/navigation.ts`
- Create: `apps/web/src/layout/navigation.test.ts`

**Interfaces:**
- Consumes: `Role`（Task 2）
- Produces: `NAV_GROUPS: NavGroup[]`，`type NavGroup = { key: string; labelKey: string; introKey: string; sections: NavSection[] }`，`type NavSection = { key: string; labelKey: string; introKey: string | null; path: string; roles: Role[] | null }`

`roles: null` 表示所有角色都看得见。

- [ ] **Step 1: 查出每一段的可见角色**

不要猜。每一段的可见角色**取自后端已有的判定**。逐个跑：

```bash
grep -rn "_require_member\|_require_admin\|_require_role\|READERS\|WRITERS" \
  packages/backend/src/tiny_hermes/*/presentation/routes.py | grep -v test
```

把每个路由用的判定记下来：用 `WRITERS`/`_require_admin` 的段 → `["workspace_admin"]`；用 `READERS`/`_require_member` 的段 → `null`。**已知数据点：渠道对 viewer 是拒绝的**（`test_binding_routes.py` 里有一条测试断言 403），可以拿它校验你的读法对不对。

把每一条的来源写成注释，格式：`// 依据：channels/presentation/routes.py 的 _require_admin`。

- [ ] **Step 2: 写失败的测试**

```ts
import { expect, test } from "vitest";

import { NAV_GROUPS } from "./navigation";

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
```

- [ ] **Step 3: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- navigation
```
Expected: FAIL，模块不存在

- [ ] **Step 4: 写定义表**

```ts
import type { Role } from "../workspace/useMyRole";

import type { MessageKey } from "../i18n/locale";

export type NavSection = {
  key: string;
  labelKey: MessageKey;
  /** 这一段自己的那句说明。`null` 表示这一页还没有——见 Task 14。 */
  introKey: MessageKey | null;
  /** 工作空间下的相对路径，不带 `/workspaces/:id/` 前缀。 */
  path: string;
  /** 能看见这一段的角色。`null` = 都能看见。
   *  **隐藏不是权限**：后端照旧拒绝，这里只是不邀请一次注定失败的点击。 */
  roles: Role[] | null;
};

export type NavGroup = {
  key: string;
  labelKey: MessageKey;
  introKey: MessageKey;
  sections: NavSection[];
};

export const NAV_GROUPS: NavGroup[] = [
  {
    key: "agents",
    labelKey: "agents",
    introKey: "agentsIntro",
    sections: [{ key: "agents", labelKey: "agents", introKey: "agentsIntro", path: "agents", roles: null }],
  },
  {
    key: "runs",
    labelKey: "runs",
    introKey: "runsIntro",
    sections: [{ key: "runs", labelKey: "runs", introKey: "runsIntro", path: "runs", roles: null }],
  },
  {
    key: "channels",
    labelKey: "channels",
    introKey: "channelsIntro",
    // 依据：Step 1 查到的判定，填进去，并把来源写成注释
    sections: [{ key: "channels", labelKey: "channels", introKey: "channelsIntro", path: "channels", roles: ["workspace_admin"] }],
  },
  {
    key: "inbox",
    labelKey: "navInbox",
    introKey: "navInboxIntro",
    sections: [
      { key: "approvals", labelKey: "approvals", introKey: "approvalsIntro", path: "approvals", roles: null },
      { key: "proposals", labelKey: "proposals", introKey: "proposalsIntro", path: "skill-proposals", roles: null },
      { key: "memory", labelKey: "memoryReview", introKey: "memoryReviewIntro", path: "memory", roles: null },
    ],
  },
  {
    key: "tooling",
    labelKey: "navTooling",
    introKey: "navToolingIntro",
    sections: [
      { key: "skills", labelKey: "skills", introKey: "skillsIntro", path: "skills", roles: null },
      { key: "http-tools", labelKey: "httpTools", introKey: "httpToolsIntro", path: "http-tools", roles: null },
      { key: "mcp-servers", labelKey: "mcpServers", introKey: "mcpServersIntro", path: "mcp-servers", roles: null },
    ],
  },
  {
    key: "records",
    labelKey: "navRecords",
    introKey: "navRecordsIntro",
    sections: [
      { key: "audit", labelKey: "audit", introKey: "auditIntro", path: "audit", roles: null },
      { key: "usage", labelKey: "usage", introKey: "usageIntro", path: "usage", roles: null },
      { key: "subjects", labelKey: "subjectData", introKey: "subjectDataIntro", path: "subjects", roles: null },
    ],
  },
  {
    key: "settings",
    labelKey: "navSettings",
    introKey: "navSettingsIntro",
    sections: [
      { key: "members", labelKey: "members", introKey: null, path: "members", roles: null },
      { key: "api-keys", labelKey: "apiKeys", introKey: null, path: "api-keys", roles: null },
      { key: "identity-providers", labelKey: "identityProviders", introKey: "identityProvidersIntro", path: "identity-providers", roles: null },
      { key: "model-endpoints", labelKey: "modelEndpoints", introKey: null, path: "model-endpoints", roles: null },
      { key: "secrets", labelKey: "secrets", introKey: "secretsIntro", path: "secrets", roles: null },
      { key: "outbound", labelKey: "outboundScopes", introKey: null, path: "outbound", roles: null },
    ],
  },
];
```

**把 `roles` 全部按 Step 1 查到的结果替换掉**，包括上面写成 `null` 的那些——`null` 是占位的读法，不是查证过的答案。

- [ ] **Step 5: 加四个新入口的文案**

`apps/web/src/i18n/zh-CN.ts`：

```ts
  navInbox: "待办",
  navInboxIntro: "有东西停在这里，等一个人点通过或拒绝。",
  navTooling: "工具与技能",
  navToolingIntro: "Agent 能绑定的东西：技能、HTTP 工具、MCP 服务。",
  navRecords: "记录",
  navRecordsIntro: "已经发生的事：谁做了什么、花了多少、某个人的数据。",
  navSettings: "设置",
  navSettingsIntro: "配一次就不太会再动的东西。",
```

`en-US.ts` 同样八个键：

```ts
  navInbox: "Inbox",
  navInboxIntro: "Things waiting for a person to approve or reject.",
  navTooling: "Tools and skills",
  navToolingIntro: "What an Agent can bind: skills, HTTP tools, MCP servers.",
  navRecords: "Records",
  navRecordsIntro: "What already happened: who did what, what it cost, one person's data.",
  navSettings: "Settings",
  navSettingsIntro: "Set once, rarely touched again.",
```

- [ ] **Step 6: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test -- navigation
pnpm --filter @tiny-hermes/web exec tsc --noEmit
```
Expected: 3 passed

- [ ] **Step 7: 提交**

```bash
git add apps/web/src/layout/navigation.ts apps/web/src/layout/navigation.test.ts apps/web/src/i18n
git commit -m "feat(web): 七个入口的定义表，每一段带可见角色与说明"
```

---

## Task 4: 分段页外壳

**Files:**
- Create: `apps/web/src/layout/GroupedPage.tsx`
- Create: `apps/web/src/layout/GroupedPage.test.tsx`

**Interfaces:**
- Consumes: `NavGroup`（Task 3）、`useMyRole`（Task 2）
- Produces: `<GroupedPage groupKey="inbox" render={(sectionKey) => ReactNode} />`

- [ ] **Step 1: 写失败的测试**

```tsx
test("画出这个人看得见的段，不画他看不见的", async () => {
  // 隐藏不是权限——后端照旧拒绝。这里只是不邀请一次注定失败的点击。
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "viewer" })),
  );
  renderGrouped("settings");

  expect(await screen.findByText(t("secrets"))).toBeVisible();
  expect(screen.queryByText(t("members"))).toBeNull();
});

test("角色还没拿到时，一段都不画", async () => {
  // 先画全部再删掉看不见的，会让 viewer 看到一次闪现的入口列表。
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => new Promise(() => {})),
  );
  renderGrouped("settings");
  expect(screen.queryByText(t("secrets"))).toBeNull();
});
```

`renderGrouped` 是这个测试文件里自己写的帮手，照 `ChannelsPage.test.tsx` 的 `renderChannels` 写法。上面第一条里 `members` 的可见角色按 Task 3 Step 1 查到的填；若查出来 `members` 对 viewer 可见，换一个对 viewer 不可见的段来断言，并在测试注释里写明换了哪个、为什么。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- GroupedPage
```
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现**

```tsx
import { Anchor, Typography } from "antd";
import type { ReactNode } from "react";

import { useT } from "../i18n/locale";
import { useMyRole } from "../workspace/useMyRole";
import { NAV_GROUPS } from "./navigation";

/**
 * 一个合并入口下面的若干段。
 *
 * **前端隐藏不构成授权。** 后端的拒绝仍然是唯一的权限判据；这里少画一段只是
 * 不去邀请一次注定失败的点击。谁把这段注释删掉之前，先想清楚下一个读代码的人
 * 会不会以为隐藏就是授权。
 *
 * 角色未知时一段都不画，而不是先画全部再删：后者会让一个 viewer 看到一次闪现
 * 的、他其实点不动的入口列表。
 */
export function GroupedPage({
  groupKey,
  render,
}: {
  groupKey: string;
  render: (sectionKey: string) => ReactNode;
}) {
  const t = useT();
  const { role } = useMyRole();
  const group = NAV_GROUPS.find((candidate) => candidate.key === groupKey);
  if (group === undefined || role === null) return null;

  const visible = group.sections.filter(
    (section) => section.roles === null || section.roles.includes(role),
  );

  return (
    <div className="grouped-page">
      <Anchor
        affix={false}
        direction="horizontal"
        items={visible.map((section) => ({
          key: section.key,
          href: `#${section.key}`,
          title: t(section.labelKey),
        }))}
      />
      {visible.map((section) => (
        <section key={section.key} id={section.key} className="grouped-section">
          <Typography.Title level={4}>{t(section.labelKey)}</Typography.Title>
          {section.introKey === null ? null : (
            <Typography.Paragraph type="secondary">{t(section.introKey)}</Typography.Paragraph>
          )}
          {render(section.key)}
        </section>
      ))}
    </div>
  );
}
```

`useT()` 的签名是 `(key: MessageKey) => string`——键名是联合类型，不是 `string`。
所以 `navigation.ts` 里 `labelKey` 与 `introKey` 的类型要写成 `MessageKey`
（`import type { MessageKey } from "../i18n/locale"`），**不要在调用处 `as any`**。
Task 3 建这个文件时就按 `MessageKey` 写。

`apps/web/src/styles.css` 末尾加：

```css
.grouped-page {
  display: flex;
  flex-direction: column;
  gap: 32px;
}

.grouped-section {
  scroll-margin-top: 96px; /* 页头是两行，锚点跳过去不能被它盖住 */
}
```

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test -- GroupedPage
```
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/layout/GroupedPage.tsx apps/web/src/layout/GroupedPage.test.tsx apps/web/src/styles.css
git commit -m "feat(web): 合并入口的分段外壳，按角色决定画哪几段"
```

---

## Task 5: 四个合并页

**Files:**
- Create: `apps/web/src/pages/InboxPage.tsx`
- Create: `apps/web/src/pages/ToolingPage.tsx`
- Create: `apps/web/src/pages/RecordsPage.tsx`
- Create: `apps/web/src/pages/SettingsPage.tsx`
- Create: `apps/web/src/pages/InboxPage.test.tsx`

**Interfaces:**
- Consumes: `<GroupedPage>`（Task 4）；十八个既有页面组件
- Produces: 四个页面组件，供 Task 6 的路由使用

- [ ] **Step 1: 写失败的测试**

只给「待办」写，因为四页是同一个模式的四次使用；另外三页由 Task 6 的路由测试覆盖到。

```tsx
test("待办把三个队列放在一页上", async () => {
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    http.get("/api/v1/approvals", () => HttpResponse.json([])),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
    http.get("/api/v1/memory-proposals", () => HttpResponse.json([])),
  );
  renderInbox();

  expect(await screen.findByText(t("approvals"))).toBeVisible();
  expect(await screen.findByText(t("proposals"))).toBeVisible();
  expect(await screen.findByText(t("memoryReview"))).toBeVisible();
});
```

三个接口路径按各页组件实际请求的填——先 `grep -n "api<" apps/web/src/pages/ApprovalsPage.tsx apps/web/src/pages/SkillProposalsPage.tsx apps/web/src/pages/MemoryPage.tsx` 看清楚再写。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- InboxPage
```
Expected: FAIL，模块不存在

- [ ] **Step 3: 写四个页面**

```tsx
// InboxPage.tsx
import { GroupedPage } from "../layout/GroupedPage";
import { ApprovalsPage } from "./ApprovalsPage";
import { MemoryPage } from "./MemoryPage";
import { SkillProposalsPage } from "./SkillProposalsPage";

/** 三个队列，一个入口。合并的依据是「有没有东西等我」这个问题比「等我的是哪
 *  一类」更常被问到——代价写在 spec §6 里。 */
export function InboxPage() {
  return (
    <GroupedPage
      groupKey="inbox"
      render={(key) =>
        key === "approvals" ? <ApprovalsPage /> :
        key === "proposals" ? <SkillProposalsPage /> :
        key === "memory" ? <MemoryPage /> : null
      }
    />
  );
}
```

`ToolingPage.tsx`（`groupKey="tooling"`，键 `skills`/`http-tools`/`mcp-servers` → `SkillsPage`/`HttpToolsPage`/`McpServersPage`）、`RecordsPage.tsx`（`groupKey="records"`，`audit`/`usage`/`subjects` → `AuditPage`/`UsagePage`/`SubjectDataPage`）、`SettingsPage.tsx`（`groupKey="settings"`，六段 → `MembersPage`/`ApiKeysPage`/`IdentityProvidersPage`/`ModelEndpointsPage`/`SecretsPage`/`OutboundScopePage`）照同一形状写。

**被嵌进来的页面组件一行都不要改。**如果某个页面自己画了一个 `<Typography.Title>` 页标题，导致标题出现两次，把**那个页面里的标题删掉**（`GroupedPage` 已经画了），其余不动。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
pnpm --filter @tiny-hermes/web exec tsc --noEmit
```
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/pages/InboxPage.tsx apps/web/src/pages/ToolingPage.tsx \
  apps/web/src/pages/RecordsPage.tsx apps/web/src/pages/SettingsPage.tsx \
  apps/web/src/pages/InboxPage.test.tsx
git commit -m "feat(web): 待办、工具与技能、记录、设置四个合并页"
```

---

## Task 6: 新路由与旧地址跳转

**Files:**
- Modify: `apps/web/src/App.tsx:144-164`
- Create: `apps/web/src/App.routes.test.tsx`

**Interfaces:**
- Consumes: Task 5 的四个页面组件

- [ ] **Step 1: 写失败的测试**

```tsx
test.each([
  ["approvals", "inbox"],
  ["skill-proposals", "inbox"],
  ["memory", "inbox"],
  ["skills", "tooling"],
  ["http-tools", "tooling"],
  ["mcp-servers", "tooling"],
  ["audit", "records"],
  ["usage", "records"],
  ["subjects", "records"],
  ["members", "settings"],
  ["api-keys", "settings"],
  ["identity-providers", "settings"],
  ["model-endpoints", "settings"],
  ["secrets", "settings"],
  ["outbound", "settings"],
])("旧地址 /%s 落在 /%s 上", async (old, group) => {
  // 这些地址可能被存过书签，也可能出现在别处。跳转是长期行为，不是过渡措施。
  renderAt(`/workspaces/${WORKSPACE}/${old}`);
  await waitFor(() =>
    expect(window.location.pathname).toBe(`/workspaces/${WORKSPACE}/${group}`));
  expect(window.location.hash).toBe(`#${old === "skill-proposals" ? "proposals" : old}`);
});
```

`renderAt` 用 `MemoryRouter` + `initialEntries`；若断言 `window.location` 在 `MemoryRouter` 下不成立，改用 `useLocation` 探针组件断言，**不要把断言弱化成「没有报错」**。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- App.routes
```
Expected: FAIL，旧地址仍渲染原页面

- [ ] **Step 3: 改路由**

`App.tsx` 里，把这十五行删掉：`usage`、`members`、`api-keys`、`model-endpoints`、`secrets`、`outbound`、`skills`、`skill-proposals`、`approvals`、`audit`、`http-tools`、`mcp-servers`、`identity-providers`、`memory`、`subjects`。

换成四个新路由加十五条跳转：

```tsx
          <Route path="inbox" element={<InboxPage />} />
          <Route path="tooling" element={<ToolingPage />} />
          <Route path="records" element={<RecordsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          {/* 旧地址。**长期保留**：一个能打开的链接不会因为新导航上线就变得
              不该打开。锚点让它落在对应的段上，而不只是那一页的顶部。 */}
          {([
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
          ] as const).map(([from, to, anchor]) => (
            <Route
              key={from}
              path={from}
              element={<Navigate to={`../${to}#${anchor}`} replace />}
            />
          ))}
```

`agents`、`agents/:agentId`、`agents/:agentId/playground`、`runs`、`runs/:runId`、`channels` 五条**不动**。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
pnpm --filter @tiny-hermes/web exec tsc --noEmit
```
Expected: 15 条跳转全过

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/App.tsx apps/web/src/App.routes.test.tsx
git commit -m "feat(web): 四个新路由，十五个旧地址跳转到对应的段"
```

---

## Task 7: 导航换成七个

**Files:**
- Modify: `apps/web/src/layout/ConsoleLayout.tsx:103-124`
- Modify: `apps/web/src/styles.css`
- Create: `apps/web/src/layout/ConsoleLayout.test.tsx`

- [ ] **Step 1: 写失败的测试**

```tsx
test("导航上是七个入口，不是十八个", async () => {
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    http.get("/api/v1/workspaces", () => HttpResponse.json([])),
  );
  renderLayout();
  const nav = await screen.findByRole("navigation");
  expect(within(nav).getAllByRole("link")).toHaveLength(7);
});

test("每个入口都带着它自己那句说明", async () => {
  // 这些句子早就写好了，只是原来要走进去之后才看得到——而需要它们的时刻
  // 是「决定走进哪里」之前。
  renderLayout();
  const inbox = await screen.findByRole("link", { name: new RegExp(t("navInbox")) });
  expect(inbox).toHaveAttribute("title", t("navInboxIntro"));
});
```

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- ConsoleLayout
```
Expected: FAIL，18 个链接

- [ ] **Step 3: 换掉导航**

`ConsoleLayout.tsx` 里 `<nav className="console-nav">` 到 `</nav>` 之间的十八行全部删掉，换成：

```tsx
        <nav className="console-nav">
          {NAV_GROUPS.map((group) => (
            <NavLink
              key={group.key}
              to={`/workspaces/${workspaceId}/${group.sections.length === 1 ? group.sections[0].path : group.key}`}
              title={t(group.introKey)}
            >
              {t(group.labelKey)}
            </NavLink>
          ))}
        </nav>
```

顶部加 `import { NAV_GROUPS } from "./navigation";`。

只有一段的入口（Agents、运行、渠道）直接指向那一段的路径，不经过合并页——给一个只有一段的页面套一层分段外壳，只会多一层没有内容的标题。

`styles.css` 里 `.app-header` 的注释现在不成立了（它说的是「一行装不下十八个」）。改成：

```css
.app-header {
  height: auto;
  min-height: 72px;
  /* 两行：品牌与账户一行，导航一行。入口从十八个降到七个之后一行本来装得下，
     但账户区（语言、主题、头像、姓名、登出）仍然会把导航挤到换行——两行是给
     导航留出整行，不是给数量留的。 */
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px clamp(20px, 5vw, 72px);
  background: rgb(255 255 255 / 92%);
  border-bottom: 1px solid #dde5e8;
}
```

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
```
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/layout/ConsoleLayout.tsx apps/web/src/layout/ConsoleLayout.test.tsx apps/web/src/styles.css
git commit -m "feat(web): 导航从十八个入口降到七个，每个带说明"
```

---

## Task 8: 「待办」上的计数

**Files:**
- Modify: `apps/web/src/layout/ConsoleLayout.tsx`
- Create: `apps/web/src/layout/useInboxCount.ts`
- Modify: `apps/web/src/layout/ConsoleLayout.test.tsx`

**Interfaces:**
- Produces: `useInboxCount(): number | null`

- [ ] **Step 1: 写失败的测试**

```tsx
test("待办上的数字是三个队列的总数", async () => {
  server.use(
    http.get("/api/v1/approvals", () => HttpResponse.json([{ id: "a" }, { id: "b" }])),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([{ id: "c" }])),
    http.get("/api/v1/memory-proposals", () => HttpResponse.json([])),
  );
  renderLayout();
  expect(await screen.findByText("3")).toBeVisible();
});

test("有一个队列读不到时不显示数字", async () => {
  // 显示「2」而其实是「2 + 读不到」，比不显示更糟：它看起来是个准确的数。
  server.use(
    http.get("/api/v1/approvals", () => HttpResponse.json([{ id: "a" }, { id: "b" }])),
    http.get("/api/v1/skill-proposals", () => new HttpResponse(null, { status: 403 })),
    http.get("/api/v1/memory-proposals", () => HttpResponse.json([])),
  );
  renderLayout();
  await waitFor(() => expect(screen.queryByText("2")).toBeNull());
});
```

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- ConsoleLayout
```
Expected: FAIL

- [ ] **Step 3: 实现**

```ts
import { useQueries } from "@tanstack/react-query";

import { api } from "../api/client";

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
    queries: ["/api/v1/approvals", "/api/v1/skill-proposals", "/api/v1/memory-proposals"].map(
      (path) => ({
        queryKey: ["inbox-count", path],
        queryFn: () => api<unknown[]>(path),
        retry: false,
      }),
    ),
  });
  if (queries.some((query) => query.data === undefined)) return null;
  return queries.reduce((total, query) => total + (query.data?.length ?? 0), 0);
}
```

三个路径按 Task 5 Step 1 查到的实际路径填。

`ConsoleLayout.tsx` 里，`NAV_GROUPS.map` 之前加 `const inboxCount = useInboxCount();`
（连同 `import { useInboxCount } from "./useInboxCount";`），然后在 `NavLink` 里
`group.key === "inbox"` 时于标签后面加：

```tsx
              {group.key === "inbox" && inboxCount !== null ? (
                <Badge count={inboxCount} className="nav-badge" />
              ) : null}
```

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
```
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/layout/useInboxCount.ts apps/web/src/layout/ConsoleLayout.tsx apps/web/src/layout/ConsoleLayout.test.tsx
git commit -m "feat(web): 待办上显示三个队列的总数，读不全就不显示"
```

---

## Task 9: 可折叠分段组件

**Files:**
- Create: `apps/web/src/forms/FormSection.tsx`
- Create: `apps/web/src/forms/FormSection.test.tsx`

**Interfaces:**
- Produces: `<FormSection title summary fields collapsible>{children}</FormSection>`
  - `title: string` — 段名
  - `summary: string` — 折叠条上显示的当前值，**必填**
  - `fields: string[]` — 这一段包含哪些表单字段名（用来判断能否折叠、以及校验失败时是否展开）
  - `collapsible: boolean` — 这一段是否**允许**折叠（全是必填的段传 `false`）

- [ ] **Step 1: 写失败的测试**

```tsx
test("折叠条上显示的是当前值，不只是段名", () => {
  // 没有这一条，折叠就等于把字段藏起来，人会不敢折叠——那就退化成了一个叫
  // 「更多设置」的抽屉。
  renderSection({ title: "计价", summary: "未设置，这个端点的花费无法计算" });
  expect(screen.getByText("未设置，这个端点的花费无法计算")).toBeVisible();
});

test("含未填必填项的段不许折叠", () => {
  // 折叠一段里藏着空的必填项，后果是点提交拿到校验错误、而出错的字段在一个
  // 看不见的地方——比不折叠更糟。
  renderSection({ collapsible: true, fields: ["context_window"], values: { context_window: undefined }, required: ["context_window"] });
  expect(screen.getByLabelText("上下文窗口")).toBeVisible();
});

test("校验失败时，出错字段所在的段自动展开", async () => {
  // 一个人不该为了看见错误先去猜它藏在哪一段里。
  const { submit } = renderSection({ collapsible: true, fields: ["context_window"], required: ["context_window"], startCollapsed: true });
  await submit();
  expect(await screen.findByLabelText("上下文窗口")).toBeVisible();
});
```

`renderSection` 是这个测试文件自己写的帮手：包一个 antd `<Form>`，把 `fields`/`values`/`required` 转成真的 `<Form.Item>`，返回一个触发提交的 `submit`。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- FormSection
```
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现**

```tsx
import { Collapse, Form, Typography } from "antd";
import { type ReactNode, useEffect, useState } from "react";

/**
 * 表单里的一段，折叠时用一行摘要代替它的全部字段。
 *
 * **摘要行不是装饰。** 没有它，折叠等于把字段藏起来，人会不敢折叠，因为不知道
 * 里面有什么——那就退化成一个叫「更多设置」的抽屉，只是把乱推进一个看不见的
 * 地方。有了它，折叠状态比展开状态信息量更大：一行就说清了这一段此刻是什么。
 *
 * **一段只有在它此刻能通过校验时才允许折叠。** 两条推论都在下面实现：新建时
 * 含未填必填项的段一律展开；校验失败时出错字段所在的段自动展开。
 */
export function FormSection({
  title,
  summary,
  fields,
  collapsible,
  children,
}: {
  title: string;
  summary: string;
  fields: string[];
  collapsible: boolean;
  children: ReactNode;
}) {
  const form = Form.useFormInstance();
  const [open, setOpen] = useState(!collapsible);
  const errors = Form.useWatch(() => form.getFieldsError(fields), form);
  const missingRequired = fields.some((name) => {
    const value = form.getFieldValue(name);
    return value === undefined || value === null || value === "";
  });

  useEffect(() => {
    if (!collapsible || missingRequired) setOpen(true);
  }, [collapsible, missingRequired]);

  useEffect(() => {
    if ((errors ?? []).some((entry) => entry.errors.length > 0)) setOpen(true);
  }, [errors]);

  return (
    <Collapse
      activeKey={open ? ["section"] : []}
      onChange={(keys) => setOpen(keys.length > 0)}
      collapsible={collapsible && !missingRequired ? "header" : "disabled"}
      items={[
        {
          key: "section",
          label: (
            <span>
              <strong>{title}</strong>
              {open ? null : (
                <Typography.Text type="secondary"> · {summary}</Typography.Text>
              )}
            </span>
          ),
          children,
        },
      ]}
    />
  );
}
```

`missingRequired` 现在把「空」等同于「缺必填」。如果某一段有可以为空的可选字段，传进来的 `fields` **只放必填的那些**——这个组件不认识校验规则，`fields` 是调用方告诉它「哪些字段决定这一段能不能折叠」。把这句话写进 props 的注释里。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test -- FormSection
pnpm --filter @tiny-hermes/web exec tsc --noEmit
```
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/forms/FormSection.tsx apps/web/src/forms/FormSection.test.tsx
git commit -m "feat(web): 可折叠表单分段，折叠时显示当前值"
```

---

## Task 10: 模型端点表单分段

**Files:**
- Modify: `apps/web/src/pages/ModelEndpointsPage.tsx:230-300`
- Create: `apps/web/src/pages/ModelEndpointsPage.test.tsx`

**Interfaces:**
- Consumes: `<FormSection>`（Task 9）

- [ ] **Step 1: 写失败的测试**

```tsx
test("新建时「这个模型的能力」是展开的——它有两个必填项还空着", async () => {
  renderEndpoints();
  await userEvent.click(screen.getByRole("button", { name: t("addEndpoint") }));
  expect(await screen.findByLabelText(t("endpointContextWindow"))).toBeVisible();
});

test("编辑一个填齐了的端点时，「计价」折叠且写着它的值", async () => {
  server.use(http.get("/api/v1/model-endpoints", () => HttpResponse.json([FILLED_ENDPOINT])));
  renderEndpoints();
  await userEvent.click(await screen.findByRole("button", { name: t("edit") }));
  expect(screen.queryByLabelText(t("pricingInput"))).toBeNull();
  expect(await screen.findByText(/CNY/)).toBeVisible();
});

test("没填计价时，折叠条上说的是这个端点的花费无法计算", async () => {
  server.use(http.get("/api/v1/model-endpoints", () => HttpResponse.json([UNPRICED_ENDPOINT])));
  renderEndpoints();
  await userEvent.click(await screen.findByRole("button", { name: t("edit") }));
  expect(await screen.findByText(t("pricingUnsetSummary"))).toBeVisible();
});
```

`FILLED_ENDPOINT` / `UNPRICED_ENDPOINT` 按 `apps/web/src/api/types.ts` 里模型端点的响应类型构造，**字段名照抄那个类型**，不要手编。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- ModelEndpointsPage
```
Expected: FAIL

- [ ] **Step 3: 分段**

把 `<Form>` 里现有的 `<Form.Item>` 按三段包起来，顺序与字段：

```tsx
  <FormSection
    title={t("endpointSectionConnection")}
    summary={`${values.model ?? ""} · ${values.base_url ?? ""}`}
    fields={["name", "base_url", "model", "credential_ref"]}
    collapsible={false}
  >
    {/* name / kind(hidden) / base_url / model / credential_ref 五个 Form.Item 原样搬进来 */}
  </FormSection>

  <FormSection
    title={t("endpointSectionCapability")}
    summary={[
      `${values.context_window ?? "?"} ${t("endpointWindowUnit")}`,
      `${t("endpointOutputPrefix")} ${values.max_output_tokens ?? "?"}`,
      values.accepts_images ? t("endpointTakesImages") : t("endpointNoImages"),
    ].join(" · ")}
    fields={["context_window", "max_output_tokens"]}
    collapsible
  >
    {/* context_window / max_output_tokens / context_accounting / accepts_images / tokenizer */}
  </FormSection>

  <FormSection
    title={t("endpointSectionPricing")}
    summary={
      values.inputPerMillion === undefined
        ? t("pricingUnsetSummary")
        : `${values.currency} ${values.inputPerMillion} / ${values.outputPerMillion}`
    }
    fields={[]}
    collapsible
  >
    {/* currency / inputPerMillion / outputPerMillion / usage_quality */}
  </FormSection>
```

`values` 用 `Form.useWatch([], form)` 取。「计价」段 `fields={[]}`：这一段整个可选，没有任何字段决定它能不能折叠。

新增文案（`zh-CN.ts` / `en-US.ts` 键名一致）：

```ts
  endpointSectionConnection: "连到哪",
  endpointSectionCapability: "这个模型的能力",
  endpointSectionPricing: "计价",
  endpointWindowUnit: "窗口",
  endpointOutputPrefix: "最多输出",
  endpointTakesImages: "收图片",
  endpointNoImages: "不收图片",
  pricingUnsetSummary: "未设置，这个端点的花费无法计算",
```

**`useT()` 不支持插值**（签名是 `(key: MessageKey) => string`），所以摘要在调用处
拼，文案里只放固定词。**不要为此给 `t()` 加插值参数**——这次改动不该顺手改动
整个控制台的翻译机制。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
```
Expected: 3 passed，其余不受影响

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/pages/ModelEndpointsPage.tsx apps/web/src/pages/ModelEndpointsPage.test.tsx apps/web/src/i18n
git commit -m "feat(web): 模型端点表单分成连接/能力/计价三段"
```

---

## Task 11: Agent 详情表单分段

**Files:**
- Modify: `apps/web/src/pages/AgentDetailPage.tsx`
- Modify: `apps/web/src/pages/AgentDetailPage.test.tsx`

- [ ] **Step 1: 写失败的测试**

```tsx
test("「能力」段折叠时说清楚绑了什么，不只是写「能力」", async () => {
  server.use(http.get("/api/v1/agents/:id", () => HttpResponse.json(AGENT_WITH_BINDINGS)));
  renderAgentDetail();
  expect(await screen.findByText(/已绑 3 个技能/)).toBeVisible();
  expect(screen.queryByLabelText(t("agentSkillPick"))).toBeNull();
});
```

`AGENT_WITH_BINDINGS` 按 `types.ts` 里 Agent 的响应类型构造，含 3 个技能、1 个只读 HTTP 工具、0 个 MCP、不允许出网。

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- AgentDetailPage
```
Expected: FAIL

- [ ] **Step 3: 分段**

四段，顺序与字段：

| 段 | `title` 文案键 | 字段 | `collapsible` |
| --- | --- | --- | --- |
| 身份 | `agentSectionIdentity` | `agentName`、`agentAlias`、`personality` | `false` |
| 模型 | `agentSectionModel` | `modelProvider`、`modelEndpoint`、`modelScenario` | `false` |
| 能力 | `agentSectionCapability` | `agentSkillPick`、`agentHttpTools`、`agentHttpWritePolicy`、`agentMcpTools`、`agentMcpWritePolicy`、`agentNetwork` | `true` |
| 对外 | `agentSectionExposure` | `endUserAccessEnabled`、`chatCompletionsEnabled`、`syncTimeoutSeconds` | `true` |

「能力」段的 `summary` 由一个本文件内的函数产出：

```tsx
function capabilitySummary(t: (key: MessageKey) => string, draft: AgentDraft): string {
  // `t()` 不支持插值，所以数字在这里拼，文案里只有固定词。
  return [
    `${t("agentSummaryBound")} ${draft.skills.length} ${t("agentSummarySkillsUnit")}`,
    `${draft.http_tools.length} ${t("agentSummaryHttpUnit")}`,
    draft.mcp_servers.length === 0
      ? t("agentSummaryNoMcp")
      : `${draft.mcp_servers.length} ${t("agentSummaryMcpUnit")}`,
    draft.network_enabled ? t("agentSummaryNetworkOn") : t("agentSummaryNetworkOff"),
  ].join(" · ");
}
```

字段名 `skills` / `http_tools` / `mcp_servers` / `network_enabled` 按 `types.ts` 里 Agent 草稿的真实字段名替换。

新增文案（`zh-CN.ts` / `en-US.ts` 键名一致）：

```ts
  agentSectionIdentity: "身份",
  agentSectionModel: "模型",
  agentSectionCapability: "能力",
  agentSectionExposure: "对外",
  agentSummaryBound: "已绑",
  agentSummarySkillsUnit: "个技能",
  agentSummaryHttpUnit: "个 HTTP 工具",
  agentSummaryMcpUnit: "个 MCP 服务",
  agentSummaryNoMcp: "无 MCP",
  agentSummaryNetworkOn: "允许出网",
  agentSummaryNetworkOff: "不允许出网",
```

四段的 `fields` 只放必填的那些（「身份」段的 `agentName` 是必填，「模型」段的 `modelEndpoint` 是必填；其余按各 `Form.Item` 的 `rules` 判断）。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
```

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/pages/AgentDetailPage.tsx apps/web/src/pages/AgentDetailPage.test.tsx apps/web/src/i18n
git commit -m "feat(web): Agent 详情表单分成身份/模型/能力/对外四段"
```

---

## Task 12: 渠道表单去重与分段

**Files:**
- Modify: `apps/web/src/pages/ChannelsPage.tsx`
- Modify: `apps/web/src/pages/ChannelsPage.test.tsx`

- [ ] **Step 1: 写失败的测试**

```tsx
test("新建和编辑用的是同一套字段定义", async () => {
  // 三个弹窗里抄三遍的后果是：改了其中一处，另外两处不知道。
  renderChannels();
  await userEvent.click(screen.getByRole("button", { name: t("bindChannel") }));
  const creating = screen.getAllByRole("textbox").map((input) => input.getAttribute("id"));
  await userEvent.click(screen.getByRole("button", { name: t("cancel") }));
  await userEvent.click(await screen.findByRole("button", { name: t("channelEdit") }));
  const editing = screen.getAllByRole("textbox").map((input) => input.getAttribute("id"));
  expect(editing).toEqual(creating);
});
```

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- ChannelsPage
```
Expected: FAIL，两组 id 不同

- [ ] **Step 3: 收敛字段定义**

把渠道绑定那五个 `Form.Item`（`agentId`、`appId`、`appSecretRef`、`encryptKeyRef`、`transport`）抽成本文件内的一个组件 `<BindingFields />`，三个弹窗都用它。签发方那五个（`issuer`、`keyMode`、`publicKey`、`jwksUrl`、`origins`）抽成 `<IssuerFields />`。

**不要把它们抽到别的文件。** 它们只有这一页用，搬出去只会多一个跳转。

抽完之后再按 Task 9 的 `<FormSection>` 分段：绑定表单一段（全必填，`collapsible={false}`），签发方表单按 `keyMode` 分成「基本」与「密钥」两段。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
```

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/pages/ChannelsPage.tsx apps/web/src/pages/ChannelsPage.test.tsx
git commit -m "refactor(web): 渠道页的字段定义收敛成一处，新建与编辑共用"
```

---

## Task 13: 表格三条界面规则

**Files:**
- Modify: `apps/web/src/pages/ChannelsPage.tsx`
- Modify: `apps/web/src/pages/ChannelsPage.test.tsx`

- [ ] **Step 1: 写失败的测试**

```tsx
test("加密密钥这一列不整列显示 UUID", async () => {
  // 一个完整 UUID 折成两行占掉整屏最宽的一格，而那个值几乎没有人会去读。
  server.use(http.get("/api/v1/channel-bindings", () =>
    HttpResponse.json([binding({ encrypt_key_ref: "2361d9ea-6591-4960-8c67-07fd26c5c38e" })])));
  renderChannels();
  const cell = await screen.findByTitle("2361d9ea-6591-4960-8c67-07fd26c5c38e");
  expect(cell.textContent).not.toContain("07fd26c5c38e");
});

test("配置和状态各占一列", async () => {
  // 「接入方式」原来一格里同时放着配置标签、状态标签和一句灰字提示。
  renderChannels();
  expect(await screen.findByRole("columnheader", { name: t("channelTransport") })).toBeVisible();
  expect(await screen.findByRole("columnheader", { name: t("channelConnection") })).toBeVisible();
});

test("可回复是文字，不是彩色标签", async () => {
  // 彩色标签只留给会变的状态；绿色在同一张表里还表示「连接中」。
  renderChannels();
  const cell = await screen.findByText(t("channelCanReply"));
  expect(cell.className).not.toContain("ant-tag");
});
```

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- ChannelsPage
```
Expected: 3 FAIL

- [ ] **Step 3: 改三处**

1. 加密密钥列的 `render` 换成：

```tsx
render: (value: string | undefined) =>
  value === undefined ? null : (
    // 表格里的 ID 一律截断，完整值挂在 title 上。这一列的值几乎没有人会读，
    // 而它原来占掉整屏最宽的一格。
    <Typography.Text title={value} style={{ maxWidth: 120 }} ellipsis copyable={{ text: value }}>
      {value}
    </Typography.Text>
  ),
```

2. 「接入方式」那一列拆成两列：原列只留配置标签（长连接／Webhook／未知值），新增一列 `channelConnection` 放连接状态标签；「重启 scheduler 后生效」那句提示留在**配置**列里（它说的是配置何时生效，不是连接状态）。

3. 「可回复」的 `<Tag color="green">` 换成 `<Typography.Text>`；不可回复的那一支同样换成文字。

新增文案：

```ts
  channelConnection: "连接状态",
```

- [ ] **Step 3b: 其它页面里的整列 ID**

§4.1 是一条规则，不是只修渠道页。找出其它整列显示 ID 的地方：

```bash
grep -n "dataIndex: \"[a-z_]*\(id\|_ref\|uuid\)\"" apps/web/src/pages/*.tsx | grep -v test
```

对每一处，用上面同一个 `ellipsis + title + copyable` 的写法改掉。**只改整列显示
原始 ID 的列**——一列显示人看得懂的名字（比如 Agent 名）不在这条规则里。

改动的每一页都要在它自己的测试文件里加一条断言：那一列的文本不含 ID 的后半段。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
```

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/pages/ChannelsPage.tsx apps/web/src/pages/ChannelsPage.test.tsx apps/web/src/i18n
git commit -m "fix(web): 渠道表格截断 ID、拆开配置与状态、可回复改文字"
```

---

## Task 14: 四个页面各补一句说明

**Files:**
- Modify: `apps/web/src/pages/MembersPage.tsx`
- Modify: `apps/web/src/pages/ApiKeysPage.tsx`
- Modify: `apps/web/src/pages/ModelEndpointsPage.tsx`
- Modify: `apps/web/src/pages/OutboundScopePage.tsx`
- Modify: `apps/web/src/layout/navigation.ts`
- Modify: `apps/web/src/i18n/zh-CN.ts`、`en-US.ts`
- Modify: `apps/web/src/layout/navigation.test.ts`

- [ ] **Step 1: 写失败的测试**

```ts
test("每一段都有一句说明", () => {
  // 这四页原来一句都没有，而它们正是「不知道是干嘛的」那一类。
  for (const group of NAV_GROUPS) {
    for (const section of group.sections) {
      expect(section.introKey, `${section.key} 还没有说明`).not.toBeNull();
    }
  }
});
```

- [ ] **Step 2: 跑它，看它红**

```bash
pnpm --filter @tiny-hermes/web test -- navigation
```
Expected: FAIL，四个 `introKey` 是 `null`

- [ ] **Step 3: 写那四句**

```ts
  membersIntro: "谁能进这个工作空间，以及各自能做什么。",
  apiKeysIntro: "给程序用的凭据。明文只在创建时出现一次。",
  modelEndpointsIntro: "这个工作空间能调用哪些模型，以及它们各自的窗口、能力和价格。",
  outboundScopesIntro: "Agent 允许访问的外部地址。不在这里的，出站代理一律拒绝。",
```

`en-US.ts` 同名四个键。

**这四句要经得起核对**：写之前分别打开那四个页面看它实际在做什么，不要照着页面名字编。若发现某一句和页面行为对不上，以页面行为为准改这句话，并在提交信息里说明改了什么。

四个页面各自在标题下加：

```tsx
<Typography.Paragraph type="secondary">{t("membersIntro")}</Typography.Paragraph>
```

`navigation.ts` 里对应四段的 `introKey` 从 `null` 改成新键名。

- [ ] **Step 4: 跑测试，看它绿**

```bash
pnpm --filter @tiny-hermes/web test
pnpm --filter @tiny-hermes/web exec tsc --noEmit
```

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/pages apps/web/src/layout/navigation.ts apps/web/src/layout/navigation.test.ts apps/web/src/i18n
git commit -m "feat(web): 成员、API 密钥、模型端点、出站范围各补一句说明"
```

---

## 全部做完之后

- [ ] **跑全量**

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"
uv run --no-sync pytest packages/backend/tests/unit packages/backend/tests/integration --ignore=packages/backend/tests/integration/sandbox -q
uv run ruff check packages/backend migrations && uv run pyright
pnpm --filter @tiny-hermes/web test && pnpm chat:test
```

- [ ] **走一遍 spec §5 的八条验收判据。**它们要的是「这条路走得通」，不是「测试过了」——第 4 条（viewer 看不到他会被拒绝的段，而后端仍然拒绝）需要一个真的 viewer 账号登录看一次，测试替代不了。

- [ ] **写验收记录** `docs/superpowers/verification/2026-09-04-console-ia-and-forms.md`，必须有「这一遍没能证明什么」与「不声称什么」两节。
