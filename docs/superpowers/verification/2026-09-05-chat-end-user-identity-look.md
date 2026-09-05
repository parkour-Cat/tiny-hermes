# 聊天页：名字、切换、分组、访客卡、默认 Agent — 验收记录 2026-09-05

> 依据：`docs/superpowers/plans/2026-09-05-paper-and-copper.md` 第 3 步。分支
> `feat/chat-end-user-identity-look`，从 `origin/main` `ee8bb65` 开，与第 2 步的分支
> 不相交（那边只动 `apps/web`，这边只动 `apps/chat-web` 和一条后端只读接口）。

## 1. 做了什么

| 项 | 落在哪 |
| --- | --- |
| 新接口 `GET /api/v1/end-user/agents`：这个会话的凭据允许的 Agent，`{alias, name}` | `runs/presentation/end_user_routes.py`；凭据的 `agents` 逐个过 `resolve_end_user_agent` 的两道门，过不了的不列也不报 |
| 标题栏显示 Agent 名字，不是别名 | `chat/AgentPicker.tsx`、`chat/useEndUserAgents.ts`、`ChatPage.tsx` |
| 标题旁的切换菜单，只列上面接口回的；只有一个时没有菜单 | 同上 |
| 会话栏按置顶 / 今天 / 昨天 / 本周 / 更早分组，五条以上出现搜索框 | `chat/sessionGroups.ts`（从设计分支来）、`chat/SessionRail.tsx`；`chat/localSessions.ts` 现在记首次打开时间，旧的纯 id 记录归到「更早」 |
| 左下角「访客」卡：外观、语言、设置入口 | `chat/UserMenu.tsx`；平台不存名字，卡上就这么说 |
| 选中的分段按钮改成纸色带铜边 | `styles.css` 的 `.choice-pills button.is-on` |
| 设置页「默认 Agent」，只在凭据允许的范围里选，只记在这台设备上 | `chat/defaultAgent.ts`、`pages/SettingsPage.tsx`、`pages/ChatHome.tsx`（`/` 直接进默认 Agent） |

**决定**（计划第 2 条）：三项都按企业凭据重做，没有改回控制台账号登录。凭据里没有
展示名，所以卡上写「访客」，不假装有账号。

## 2. 测试

| | 结果 |
| --- | --- |
| 后端 `test_end_user_agents.py`（先红后绿） | 2 passed；ruff、pyright 0 |
| chat-web `tsc --noEmit`、`eslint . --max-warnings 0` | 0 |
| chat-web `vitest run` | 17 个文件 65 个用例全过（新增 AgentPicker 4 条、sessionGroups、默认 Agent 2 条） |

后端全量与 e2e 交给 CI。

## 3. 真机走查

本机栈（`api` 容器按本分支重建）+ 本分支 vite（聊天页 :5174）。签一张凭据，
`agents` 列了三个别名：两个已发布且开了终端用户入口的 Agent，加一个不存在的
`ghost`。Playwright 驱动，1440×900。

| 看什么 | 结果 |
| --- | --- |
| 标题 | 「客服 Concierge」，不是 `concierge-compare` |
| 切换菜单 | 恰好两项：客服 Concierge、简历润色；`ghost` 没出现 |
| 切换后 | 地址变成 `/resume-helper`，标题变成「简历润色」 |
| 会话栏 | 两条对话之后出现「今天」一组 |
| 访客卡 | 「访客 / 这个入口不保存你的名字，只认企业签发的凭据。」+ 外观、语言、设置 |
| 设置页 | 「默认 Agent」两张卡，点「简历润色」后带铜边 |
| 再开 `/` | 直接落在 `/resume-helper` |

截图在会话的临时目录里，没有进仓库。

## 4. 这一遍没能证明什么

- 搜索框：走查只造了两条对话，没到五条，搜索框没在真机上出现过；只有单元
  测试（`sessionGroups.test.ts` 的 `filterSessions`）。
- 「昨天 / 本周 / 更早」三组：真机只见到「今天」；分组逻辑只有单元测试。
- 凭据里点了 Agent 但工作空间后来关掉入口的情况：接口测试覆盖了「没开」，
  没覆盖「开过又关」——它走的是同一个门，但没在真机验。
- 深色模式下的访客卡与设置页卡片：没截。
- e2e `end-user.spec.ts`：没在本机跑，等 CI。它断言的是地址与输入框标签，标题
  改成名字不该影响它。

## 5. 不声称什么

- 不声称「默认 Agent」跨设备生效：它只在 localStorage 里。
- 不声称切换菜单反映凭据换发后的实时范围：`caller.agents` 是换会话那一刻的快照
  （`create_run` 的 docstring 说明了为什么改不了）。
- 不声称访客卡上能显示这个人是谁：规格 §4.5.1 让平台不存名字，这是设计。
- 不声称 `ChatHome` 对「会话过期」和「从没有会话」给出不同答案：两者都回到
  「等待接入」，因为对这个页面来说都是同一件事——需要宿主页重新给一张凭据。

## 6. 留在本机栈上的东西

工作空间「设计对比 2026-09-05」多了一个已发布的 Agent「简历润色」（`resume-helper`），
和这次走查的两条会话；两个一次性账号还在。两步都合并之后一起清。
