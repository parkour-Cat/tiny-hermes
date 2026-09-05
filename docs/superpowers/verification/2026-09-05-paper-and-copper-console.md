# 控制台换上纸与铜 — 验收记录 2026-09-05

> 依据：`docs/superpowers/plans/2026-09-05-paper-and-copper.md` 第 2 步。设计稿是
> `feat/chat-identity-seam`（已推到 origin 归档，本地 worktree
> `.worktrees/chat-identity-seam`）。分支 `feat/paper-and-copper`，从 `origin/main`
> `ee8bb65` 开。

## 1. 做了什么

只动外壳、主题、样式和列表行；页面逻辑、接口、表单分段、导航定义一行没改。

| 项 | 落在哪 |
| --- | --- |
| 人物 lockup：登录页整幅、侧栏与空状态圆形小标、favicon | `apps/web/public/` 三张 PNG（从 chat-web 复制）、`index.html`、`ui/HermesMark.tsx` |
| 纸色 + 铜色主题，亮暗两套 | `layout/ConsoleTheme.tsx` 的 `consoleDesignToken`、`styles.css` 的 `--th-*` |
| 侧栏 + 细顶栏 | `layout/ConsoleChrome.tsx`（新）、`ConsoleLayout.tsx`、`WorkspacesPage.tsx` |
| 眉标 + 大标题 | `ui/PageHeading.tsx`（新）；Agent、任务、渠道、Agent 详情、任务详情、工作空间、四个合并页 |
| 登录页文案：眉标「企业运行台」、标题「把 Agent 关进限额、审计和可暂停的任务里」、副句 | `LoginPage.tsx` 的 `PublicShell`（Bootstrap 页共用）、`appKicker` / `appTagline` / `appAside` |
| 灰底状态标签，显示人读的词 | `ui/StatusTag.tsx`、`status.ts`（新，从分支来，加了 published / unpublished）；Agent 列表、工作空间、任务列表与详情、渠道表格 |
| 统一空状态 | `ui/EmptyState.tsx`；18 个页面的 33 处 `<Empty>` 全换 |
| 工作空间列表不印 UUID；列表整行可点 | `WorkspacesPage.tsx`；`.workspace-row h4 a::after` |

## 2. 测试

| | 结果 |
| --- | --- |
| `tsc -b`、`eslint . --max-warnings 0` | 0 |
| `vitest run` | 36 个文件 295 个用例全过 |

改了 7 条测试：3 条主题测试原来拿 Ant Design 默认算法的底色比对，现在比
`consoleDesignToken()` 给的纸色；4 条任务页测试原来找 `running` / `completed`
这样的协议码，现在找「执行中」「已完成」。

后端没动。CI 会跑全量。

## 3. 真机走查

本机栈 API（:8000）+ 本分支 vite（:5173），Playwright 截 1440×900，一次性账号
`walk-admin@example.com`，工作空间「设计对比 2026-09-05」。看过的页面：登录、
工作空间列表、Agent 列表、Agent 构建器、任务、设置组、渠道；深色各一张。

与设计分支的截图并排比：登录页、工作空间、Agent 列表已经一致（人物 lockup、
侧栏、眉标、铜色标题、灰底标签、无 UUID）。构建器是分段而不是页签——这是
计划里的决定 1。深色模式下侧栏、标签、列表行都按深色那套变量走，没有白块。

## 4. 这一遍没能证明什么

- 窄于 900px 时侧栏折成一行横排的样子——只截了 1440。
- 任务详情、调试台、运行时间线：只跟着主题变了色，没有逐页看。
- 平台管理员之外的角色看到的侧栏——入口本来就不按角色隐藏，合并页里的段才隐藏，
  这一轮没动那部分。
- e2e：没在本机跑（要隔离栈），等 CI 的 compose-e2e。导航入口的名字没变，e2e 靠
  名字点，预期不用改。

## 5. 不声称什么

- 不声称这套外观在别的屏幕密度或字体缺失时的样子：Syne 与 Noto Sans SC 从 Google
  Fonts 加载，加载不到时退到系统字体，没有验证退化后的排版。
- 不声称「整行可点」对键盘用户等价：伪元素撑满的是指针可点的区域，Tab 顺序仍是
  标题链接和行尾按钮各一个。
- 不声称 `StatusTag` 覆盖了所有状态码：表里没有的码原样显示。

## 6. 留在本机栈上的东西

一次性账号 `walk-admin@example.com` / `walk-viewer@example.com`，工作空间「设计对比
2026-09-05」（一个已发布的 Agent、一个未发布的、一个签发方）。第 3 步还要用，
做完一起清。
