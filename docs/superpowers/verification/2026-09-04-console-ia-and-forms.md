# 控制台信息架构与表单分段 — 验收记录 2026-09-04

> 依据：`docs/superpowers/specs/2026-09-04-console-ia-and-forms-design.md` §5 的
> 八条验收判据；实施计划 `docs/superpowers/plans/2026-09-04-console-ia-and-forms.md`。
> 分支 `feat/console-ia`，基于 `origin/main` `e27f973`，16 个提交。

## 1. 全量测试

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| 后端单元 + 集成（不含 sandbox） | `uv run --no-sync pytest packages/backend/tests/unit packages/backend/tests/integration --ignore=…/sandbox` | 3223 passed，24 分 59 秒 |
| 后端静态 | `ruff check packages/backend`、`pyright packages/backend` | 都是 0 |
| 控制台 | `tsc -b`、`eslint . --max-warnings 0`、`vitest run` | 36 个文件 295 个用例全过 |
| 聊天页 | `pnpm chat:test` | 15 个文件 53 个用例全过 |

本地测试库在 `localhost:5432`（计划里写的 55432 是隔离栈的口，本机没有起它）。

## 2. 真机走查

**环境。** 本机 dev 栈（`deploy/compose/compose.yaml`，API `:8000`）+ 本分支的 vite
dev 控制台（`:5173`，`/api` 代理到 `:8000`）。栈里 `api` 容器按本分支重建过一次——
它原来跑的是 `main` 的镜像，没有 `GET /workspaces/{id}/members/me`，合并页在它面前
是空的。**其余容器（web、scheduler、worker、controller）仍是 `main` 的镜像。**

**账号。** 两个一次性本地账号 `walk-admin@example.com`（平台管理员）与
`walk-viewer@example.com`，由脚本直接写进 `users` / `auth_identities`（同集成测试
`_seed_user` 的做法，密码走 `pwdlib` 的推荐哈希）。走查工作空间「IA 走查
2026-09-04」由 walk-admin 建，viewer 以 `viewer` 角色被请进去。

**驱动。** Playwright 脚本（两个用例，Chromium，1280 宽），不是人手点的。它记录
的事实如下；每一条都能在本机重跑得到。

### 八条判据

| # | 判据 | 结果 |
| --- | --- | --- |
| 1 | 导航 7 个入口 | ✅ `Agent 任务 渠道 待办 工具与技能 记录 设置`，管理员与 viewer 都是 7 个 |
| 2 | 旧地址落到新位置的段 | ✅ 15 个旧路径逐一打开，都 `replace` 到 `…/{组}#{段}`，且 `section#{段}` 可见（`redirects` 记录见 §2.1） |
| 3 | 待办数字 = 三个队列之和 | ⚠️ 走查账号能读到的唯一工作空间三个队列都是 0，页面上没有数字——**只证明了「0 时不显示」**；非零的加法只有单元测试证明 |
| 4 | viewer 看不到会被拒绝的段，后端仍拒绝 | ✅ 见 §2.2 |
| 5 | 编辑首屏只展开「连到哪」；新建时「能力」展开 | ⚠️ 新建 ✅（连到哪、能力都展开且不可折叠；计价折着）；**编辑没在真机走**——本机栈一个模型端点都没有，走查账号不想往平台级列表里塞一个 |
| 9 | 清空必填项提交，出错的段展开 | ⚠️ 同上，只在单元测试里走过（`FormSection.test.tsx`「校验失败时自动展开」） |
| 6 | 「计价」空着时折叠条说「花费无法计算」 | ⚠️ 折叠条 ✅「未设置，这个端点的花费无法计算」；**用量页那一半没走**（没有端点就没有用量） |
| 7 | 渠道表格没有一格因 UUID 换行 | ✅ 在走查工作空间造了一条绑定（加密密钥引用是一个 UUID），表格十列每格高 41px，同一行 |
| 8 | 四个页面顶部各有一句说明 | ✅ `/settings` 上四句都可见 |

### 2.1 旧地址

15 条，形如 `approvals → /inbox#approvals`、`skill-proposals → /inbox#proposals`、
`memory → /inbox#memory`、`skills|http-tools|mcp-servers → /tooling#…`、
`audit|usage|subjects → /records#…`、`members|api-keys|identity-providers|model-endpoints|secrets|outbound → /settings#…`。

### 2.2 viewer

页面上画出的段：

| 组 | viewer 看到 | 没画 |
| --- | --- | --- |
| 设置 | members、model-endpoints、outbound | api-keys、identity-providers、secrets |
| 待办 | approvals、proposals | memory |
| 记录 | audit、usage | subjects |
| 工具与技能 | skills、http-tools、mcp-servers | — |

同一个 viewer 的会话直接打后端：`/service-accounts`、`/secrets`、`/oidc/providers`、
`/memories/pending`、`/channel-bindings` 都是 **403**；`/members/me` 回 `{"role":"viewer"}`。

「渠道」对 viewer 仍是导航上的一个入口（七个入口是全局约束，它整组只有一段），
点进去是「无权查看」的提示，不是空表。

## 3. 走查抓到的、测试没拦住的

三处，都在 `92541b9`：

1. `useMyRole` 请求 `members/me` **没带 `X-Workspace-Id`**，后端回 400，于是每一个
   合并页都是空的。msw 桩不检查这个头，二十多个单元测试全绿。
2. `useInboxCount` 同样没带头；带上之后又发现 `/api/v1/approvals` 回的是
   `{items, has_more}` 不是数组，`/api/v1/skill-proposals` 不带 `status=pending`
   时连已决定的也回。桩里写的是数组。
3. 走查脚本自己也犯了第 2 条的错，改过之后才走通。

对应的桩现在会像真路由一样：没有头就拒绝，回真实的形状。

## 4. 与计划不同的地方

- **Agent 详情的名称与别名**并进了「身份」段，但保留自己的「保存」按钮，走自己的
  PATCH——改名不该长出一个草稿修订（原测试「saving a name sends a patch, not a new
  draft revision」照旧通过）。
- **模型端点**页原来有三个独立的编辑入口（调整窗口、设定价格、接受图片开关），并
  进同一张编辑表单；PATCH 只带改动的字段。**新建表单不再预填 128000 / 4096**：
  spec §5.5 要求「能力」段在新建时展开，而一个猜出来的窗口若对这个模型是错的，比
  空着更糟。
- **表格 ID 截断**截的是文本（前 8 位 + …，title 与复制里是完整值），不是 CSS
  省略号：省略号只在有宽度可量时生效，而一个 36 位 UUID 在窄格里会折行。阈值 20
  个字符以下不截。
- **四句说明**只写在导航定义里，由 `GroupedPage` 画在各段标题下；三个页面自己原有
  的那句（密钥只出现一次、出站只能收窄、端点由平台批准）说的是规则，留着。模型端
  点那句从计划的「这个工作空间能调用哪些模型」改成「平台接入的模型服务……工作空
  间只能从这里面选」——这一页是平台级的。
- 顺手删掉了签发方表格里抄来的「可回复」列：签发方没有应用密钥，那一列每行都是
  「仅接收」。

## 5. 这一遍没能证明什么

- 待办数字在**非零**时与三个队列之和相等——真机上只见到 0。
- 模型端点的**编辑**态在真机上的样子（首屏只展开「连到哪」、折叠条上的值、清空必
  填后自动展开）——本机栈没有端点。
- 「计价」空着时**用量页**的显示与折叠条的说法一致。
- 窄于 1280 的屏幕上渠道表格是否仍不换行——只量了一个宽度。
- 一个真人（而不是脚本）在这套导航里找东西是不是更快——这是 spec 的动机，而这一
  遍没有可比的对照。
- 除 `api` 以外的容器仍在跑 `main` 的镜像；scheduler / worker 与本分支无关，但这
  一遍没有证明它们与重建后的 `api` 一起没有问题。

## 6. 不声称什么

- 不声称前端隐藏是权限：判据 4 的后半（后端仍拒绝）是权限的全部；前端少画一段只
  是不邀请一次注定失败的点击。`GroupedPage` 与 `navigation.ts` 里都写着这句。
- 不声称走查覆盖了所有角色：只走了 `workspace_admin`（兼平台管理员）与 `viewer`；
  `developer` 的可见性只靠 `navigation.test.ts` 与 2026-09-04 对后端的一次探测
  （developer 在 memories/pending 与 oidc/providers 上 403）。
- 不声称旧地址跳转对**带查询参数**的地址也成立——15 条都是裸路径。
- 不声称 FormSection 对「折叠时值可以为空的可选段」处理得对：`fields` 只放必填，
  文档里写了，但没有一段真的把可选字段放进去试过。

## 7. 留在本机栈上的东西

- 两个账号 `walk-admin@example.com` / `walk-viewer@example.com`（密码在走查脚本里）；
- 工作空间「IA 走查 2026-09-04」，含一条工作空间密钥 `walk-encrypt-key`、一个未发布
  的 Agent `walk-agent`、一条飞书绑定（app_id `cli_walk`）；
- `api` 容器的镜像是本分支构建的。

都可以删；只影响本机。
