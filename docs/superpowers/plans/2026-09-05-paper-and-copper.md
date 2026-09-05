# 把 feat/chat-identity-seam 的外观移植到现在的 main

> 由来：2026-09-05 把 main 与 8 月 17 日的设计分支 `feat/chat-identity-seam` 逐页截图对比
> （截图与勾选清单在会话产物「tiny-hermes 外观取舍」里），用户勾了要搬的 17 项。分支基于
> 8 月 13 日的 main，之后 main 在 chat-web 上又走了 18 个提交、控制台 50 个，全是按规格
> 加的功能；git 合并没有冲突，但那只说明改的不是同一行。**所以不合分支，把设计当设计稿，
> 在现在的 main 上重做。** 分支推到 origin 归档。

## 这份计划替产品做的决定

1. **构建器保留可折叠分段，只换外观。** 用户原本勾了「页签」，问过之后改为分段：规格 §3 的三条
   （折叠条写当前值、缺必填的段不许折、出错的段自动展开）页签做不到。规格不改。
2. **聊天页的三项按企业凭据重做，不改回控制台账号登录。** 智能体切换只在凭据 `agents` 列出的
   范围内；左下账号卡显示凭据里的名字（没有就显示「访客」），装主题、语言和设置入口；默认
   Agent 只在那个范围里记。规格 §4.5 不改。这需要一个新的只读接口：终端用户会话能问
   「我能找哪些 Agent，它们叫什么」。
3. **主语仍叫「Agent」。** 分支叫「智能体」，用户选了不搬。登录页那句因此写成
   「把 Agent 关进限额、审计和可暂停的任务里」。
4. **工作空间列表不再显示 UUID。** 这本来就是 §4.1 该管的，不算外观取舍。

## 第 2 步：控制台外观（一个 PR）

分支 `feat/paper-and-copper`，从 `origin/main` 开。改的是外壳、主题、样式和列表行，
**不动任何页面的逻辑、接口和表单分段。**

| # | 做什么 | 来源（分支文件） | 落到 main 的哪里 |
| --- | --- | --- | --- |
| 1 | 人物 lockup：登录页整幅，侧栏与空状态圆形小标，favicon | `ui/HermesMark.tsx`、`public/*.png` | `apps/web/public/`（三张 PNG 从 `apps/chat-web/public` 复制）、`index.html`、`ui/HermesMark.tsx` |
| 2 | 纸色 + 铜色主题，亮暗两套 | `layout/ConsoleTheme.tsx` | 同名文件，token 整体替换；`styles.css` 的 `--th-*` 变量 |
| 3 | 侧栏 + 细顶栏 | `layout/ConsoleChrome.tsx` | 新文件 `ConsoleChrome.tsx`；`ConsoleLayout.tsx` 改成用它，7 个入口进侧栏，待办角标照旧；`WorkspacesPage` 也套它 |
| 4 | 页面眉标 + 大标题 | `PageHeading` | 新 `ui/PageHeading.tsx`；独立页（工作空间、Agent、任务、渠道、Agent 详情、任务详情、调试台）与 `GroupedPage` 的组标题用它 |
| 5 | 登录页：lockup、眉标「企业运行台」、标题、副句 | `pages/LoginPage.tsx` 的 `PublicShell` | `LoginPage.tsx`、`BootstrapPage.tsx` 共用 `PublicShell`；三个新文案键 |
| 6 | 列表行：铜色标题、灰底状态标签、整行可点 | `ui/StatusTag.tsx`、`styles.css` 的 `.workspace-row` | `StatusTag.tsx` 新文件；Agent 列表、工作空间列表、任务列表、渠道表格的状态标签 |
| 7 | 空状态统一带人物小标 | `ui/EmptyState.tsx` | 新文件；替换所有 `<Empty description=…>` |
| 8 | 工作空间列表不显示 UUID | — | `WorkspacesPage.tsx` |

**验收（真机）：** 登录页、工作空间、Agent 列表、构建器、任务、设置四组页面各截一张，
与设计分支的截图并排；深色模式各一张；e2e 全绿（导航入口名字没变，e2e 不用改）。

## 第 3 步：聊天页（一个 PR）

分支 `feat/chat-end-user-identity-look`，在第 2 步合并之后开。

| # | 做什么 | 依赖 |
| --- | --- | --- |
| 1 | 标题栏显示 Agent 名字，不是别名 | 新接口 `GET /api/v1/end-user/agents`：这个会话的凭据允许的 Agent，`{alias, name}` 列表。只读，走终端用户会话 cookie |
| 2 | 标题旁的切换菜单，只列上面那个接口回的 | 同上；分支 `AgentPicker.tsx` 的外形 |
| 3 | 会话栏按今天 / 昨天 / 本周 / 更早分组，五条以上出现搜索框 | 分支 `sessionGroups.ts`、`SessionRail.tsx`；会话时间从 `GET …/sessions/{id}` 或本机记录里来——**要核对终端用户能不能拿到 created_at** |
| 4 | 左下账号卡：名字 + 展开箭头，里面是外观、语言、设置入口 | 分支 `UserMenu.tsx` 的外形；名字来自凭据的展示名（若凭据不带，显示「访客」） |
| 5 | 外观 / 语言用纸色分段按钮 | 分支 `.choice-pills` 样式 |
| 6 | 默认 Agent：在凭据允许的范围里记一个，只记在这台设备上 | 1 |

**验收（真机）：** 用签发的凭据进聊天页，切换 Agent、看分组与搜索、改主题语言、
重开标签页回到默认 Agent。

## 第 2 步做完之后再决定的

- 侧栏在窄屏上的折法（分支是变成一行横排）。
- 任务详情、调试台、运行时间线的样式细节——这一轮只让它们跟着主题变色，不重排。
