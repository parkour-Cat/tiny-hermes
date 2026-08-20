# tiny-hermes M2 Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 分五个可独立运行、可自动验证的阶段交付 tiny-hermes 0.2 Agent Preview：一个能自己判断任务是否完成、长时间运行不撑爆上下文、能加载企业技能、能记住用户、能并行委派子 Agent 的轻量 Hermes 内核。

**Architecture:** 不新增发布单元，也不新开一条与 0.1 并行的执行路径。M2 的每一项能力都长在已经跑通的 Run 主链路上：同一个 Worker、同一个状态机、同一套检查点与事件。唯一新增的进程是独立 `egress-proxy`，它是 MCP 与 HTTP 工具的前置条件而不是它们的配套。

**Tech Stack:** 沿用 M1（Python 3.12、uv、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL、Redis、MinIO、React、TypeScript、Vite、Ant Design、Docker Compose、pytest、Vitest、Playwright），新增 PostgreSQL 全文检索与本地 tokenizer。

---

## 1. 路线图使用规则

本文只固定阶段边界、先后依赖与阶段出口，不替代逐文件实施计划。每进入一个阶段前，基于前一阶段已经跑通的代码再写该阶段的细化计划。

权威输入：

- 产品范围与验收：`docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4，M2 见 §12–§16、§26 与 §27.2。
- 0.1 的实际形态：`docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` 与四个阶段的验收记录。
- 终端用户入口的空缺与未决问题：`docs/superpowers/research/2026-08-16-end-user-entry.md`。

沿用 M1 的共同完成规则（先写会失败的测试、每阶段可从空库启动、前端不得用假数据伪装后端、PostgreSQL 是状态真相、跨工作空间查询由服务端限定范围、每阶段保存验收记录），并补三条 M2 专有的：

- **模型的判断只是建议**（§531）。Goal 结论、记忆写入、技能提案和子 Agent 委派都必须由服务端复核状态、预算与权限后才生效。
- **每一种新增的暂停都必须可恢复**，并且恢复不重置任何累计安全阀。
- **能力增加不等于权限增加**。技能、MCP 工具、子 Agent 都不能成为绕过 §16.2 两次权限检查的新入口。

## 2. 0.1 留下的地基与缺口

M2 的多数工作是把 0.1 已经预留但从未被触发的结构第一次接上电。

| 能力 | 0.1 现状 | M2 要做的 |
|---|---|---|
| 一轮模型循环 | `runs/domain/slice_policy.py` 的 `decide_after_round()` 用 Provider 的 `StopReason.COMPLETED` 结束 Run | Goal 判断器取代它成为 `done` 的来源 |
| `waiting_external` | 状态与 `wait_kind` 字段都在，但 `runs/application/scheduler.py:281` 明确记着「阶段 2B 从不产生 `waiting_external`」 | Goal 的 `wait` 与父 Run 等待子 Run 第一次真正使用它 |
| `waiting_approval` | 状态存在，无生产用法 | 两类人工审批第一次使用 |
| 根预算 | `budget_root_run_id` 已在重试链上共享 | 整棵子 Agent 任务树接入同一范围 |
| 调用主体 | `CallerIdentity(caller_type, caller_id)`，`CallerType` 只有 `user` 与 `service_account` | 私有记忆按此主体隔离；第三种主体落地时不改表 |
| 出站 | `outbound/` 的 `SafeOutboundClient`，进程内强制加架构测试 | 升级为独立 `egress-proxy` |
| 上下文 | 无分段预算、无裁剪顺序、无压缩 | 交付 §7.4.2 全套 |
| 技能 / 记忆 / 子 Agent / 审批队列 | 无代码 | 本里程碑交付 |

## 3. 阶段依赖

```mermaid
flowchart LR
    A["M2A：Goal 循环与上下文预算"] --> B["M2B：企业技能目录"]
    B --> C["M2C：egress-proxy、外部工具与审批"]
    C --> D["M2D：双层记忆与会话搜索"]
    D --> E["M2E：一层并行子 Agent"]
    E --> M2["0.2 Agent Preview 验收"]
```

不能倒置的原因：

- **A 必须最先。** 后面每一项要么给提示词加内容（技能、记忆），要么给 Run 加轮次（子 Agent、审批等待）。在一个只跑一轮、且没有上下文预算的循环上加这些东西，第一天就会撞窗口，而且撞的时候没有裁剪顺序可依。
- **B 在 C 之前。** 技能是工作空间内部的离线内容，不引入新的网络面；它正好消费 A 刚建好的「技能摘要」预算段，并且让「提案 → 扫描 → 差异 → 批准 → 不可变版本」这条审批路径先在低风险对象上跑通，C 的工具审批是它的高风险版本。
- **C 在 D、E 之前。** §586 要求子 Agent 权限是父权限与委派策略在工具、文件、网络、密钥、技能、记忆六个面上的交集。这个交集必须在工具面定型之后计算一次，否则 MCP 与 HTTP 工具上线时要重新推导整套委派规则。§26 也明确 `egress-proxy` 先于 MCP、OpenAPI/HTTP 工具、两类审批和费用安全阀。
- **D 与 E 可以对调。** 二者互不依赖，只有 §27.2.3（子 Agent 读不到未授权的父私有记忆）同时需要两者，它始终落在后一个阶段。这里把记忆排在前面，是因为它是终端用户能直接感知的价值，而子 Agent 更接近基础设施；如果第 9 节的产品假设被否掉、记忆要等身份决定，就把两阶段对调，其余不受影响。

## 4. 阶段一 M2A：Goal 循环与上下文预算

**可运行成果：** 给 Agent 一个「修好这三个文件的错误并让测试通过」这类任务，它自己判断继续、等待还是完成，连续执行多轮；对话长到超过端点窗口时按固定顺序裁剪并做结构化压缩，压缩失败不丢原文；撞上安全阀时进入可恢复的 `paused(limit)`。

**包含：**

- Goal 判断器：`done` / `continue` / `wait` 三种结论，以及服务端对状态、预算和权限的复核。
- 完成条件声明（§12.2）：预期产物、验证命令、必须满足的约束、允许操作的范围、停止条件。验证命令只在沙箱内执行。
- `continue` 生成的下一轮指令进入同一 Run 的下一个检查点步，不新建 Run，不改 Session FIFO 语义。
- `wait` 第一次产生 `waiting_external`，带 `wait_kind` 与 `wait_deadline_at`，释放 WorkerLease 并销毁沙箱；Scheduler 那条从未被触发的等待超时扫描第一次有输入。
- 最大连续轮数安全阀，接入已有的根预算范围。
- `ModelEndpoint` 声明输入窗口、最大输出与 `context_accounting`（`shared` / `separate`），按端点能力计算而不按 Provider 名称猜测。
- §7.4.2 的分段上下文预算：每段 `min_tokens` / `target_tokens` / `max_tokens`、是否可裁剪、裁剪优先级；平台默认值与硬上限，AgentVersion 可在硬上限内覆盖。
- 发布时校验：目标值装不下但不可裁剪内容仍可装下时返回 `context_budget_unsatisfied` 与逐段缩放建议，建议不静默生效。
- 固定裁剪顺序：旧工具大结果 → 未命中技能摘要 → 低相关记忆 → 旧会话结构化压缩；工具调用与工具结果不拆开。
- 结构化压缩：记录覆盖的消息范围与原始引用；失败保留原文；原文仍装不下则 `paused(context_overflow)`，不静默删除中间消息。
- 控制台 Run Detail 显示轮次、Goal 判断、上下文与压缩事件（§20.3、§920）。

**明确不包含：** 子 Agent、记忆、技能、MCP/HTTP 工具、`egress-proxy`、货币费用硬上限（沿用 M1 的 Token、次数与时间安全阀）。

**逐文件计划触发条件：** 0.1 的四阶段验收记录齐全，`main` 上的控制台文案与确认改动已合并。

**出口检查：**

- 一个需要三轮以上工具调用的任务由 Goal 判断器判 `done` 结束，而不是靠 Provider 的 stop reason。
- 模型判 `done` 但验证命令失败时，服务端不接受该判断，Run 继续或按规则暂停。
- 最大连续轮数触发后进入 `paused(limit)` 并写 `run_limit_reached`；由有权限主体扩大预算后可继续，累计值不重置。
- `wait` 的 Run 确实释放了租约并销毁了沙箱；到期后按 `paused(external_timeout)` 处理。
- 把端点窗口缩到只装得下不可裁剪内容：发布被拒并给出逐段建议；运行时同样输入进入 `paused(context_overflow)`。
- 压缩之后能从原始引用取回被覆盖的消息范围。

## 5. 阶段二 M2B：企业技能目录与自我改进

**可运行成果：** 管理员上传或从 Git 导入一个技能包，扫描通过后发布为不可变版本并绑定给 Agent；Agent 的系统提示词里只出现技能名称与简介，命中任务后才加载 `SKILL.md`；Agent 提出一个补丁，经差异、扫描与人工批准后成为新版本，并明确切换过去。

**包含：**

- `Skill`、不可变 `SkillVersion`、`SkillProposal` 数据模型，含来源、内容哈希与扫描结果。
- 人工上传与 Git 导入；导入的出站请求走当时的强制出站面（此阶段仍是 `SafeOutboundClient`）。
- 平台内置技能与工作空间私有技能两级目录；搜索、查看、绑定、停用、回滚。
- 渐进加载（§15.2）：摘要进入 A 阶段建好的「技能摘要」段并受该段预算约束，正文在命中后才加载。
- 自我改进的六步固定路径（§15.3）；Agent 不得修改正在生产运行的技能版本。
- 静态扫描与权限检查作为发布前置条件。
- 控制台：技能目录、提案差异、批准与拒绝。

**明确不包含：** 公共技能市场、技能内的任意网络访问（等 C 阶段）、任何形式的自动批准。

**出口检查：**

- 未绑定技能的 Agent 提示词里没有它的摘要；绑定后也只有名称与简介，正文在命中前不进上下文。
- 技能摘要超出该段 `max_tokens` 时优先移除未命中技能，不截断单条摘要。
- 未批准的 `SkillProposal` 不产生新版本；批准后旧版本仍可回滚。
- 扫描不通过的提案不能发布。
- 同一内容哈希重复导入不产生第二个版本。

## 6. 阶段三 M2C：egress-proxy、外部工具与审批

**可运行成果：** 所有出站请求经过一个独立进程，强制平台、工作空间、Agent、Run 四层出站范围的交集；在它上线之后 MCP 与 OpenAPI/HTTP 工具才第一次可用，并各自经过两次权限检查；高风险调用进入正确的审批队列，参数变化使已有审批失效。

**包含：**

- 独立 `egress-proxy` 进程与 Compose 服务；API、Worker、Scheduler 这些平台可信进程的出站也必须经过它。
- 四层出站范围模型与交集计算，沿用 M1 已验证的地址拒绝与重定向复查规则。
- 架构测试升级：任何绕过 proxy 直连的代码路径使检查失败。
- MCP 工具：schema 拉取与预算；超预算写 `tool_schema_budget_exceeded` 并进入 `paused(tool_budget_exceeded)`。
- OpenAPI/HTTP 工具的注册与执行。
- §16.2 两次权限检查覆盖全部新工具。
- 两类人工审批（§16.3）：治理审批与用户确认。`waiting_approval` 第一次有生产用法；参数变化使原审批失效；无 EndUser 的 ServiceAccount Run 只能使用发布时已批准的预授权或治理审批。
- 货币费用安全阀与 `ModelPricingVersion`（§12.4）；Run 创建时固定引用当时的价格版本。

**明确不包含：** 审批的飞书或移动端通知、审计查询与导出、完整治理审批队列界面（均为 M3）。

**出口检查：**

- 关闭 `egress-proxy` 后所有出站工具与模型调用失败，且没有任何路径回退到直连。
- 工作空间允许、Agent 未允许的目标被拒；Run 级范围只能收窄不能放宽。
- MCP schema 超预算的 Run 进入 `paused(tool_budget_exceeded)`，恢复后不重复扣预算。
- 终端用户身份不能批准治理操作。
- 审批通过后修改工具参数，原审批失效并重新排队。
- `usage_quality=unavailable` 的端点上货币硬上限被禁用，而时间、调用次数与单次最大输出仍强制；未知费用不记为 0。

## 7. 阶段四 M2D：双层记忆与会话搜索

**可运行成果：** Agent 在多次会话之间记得同一个人的偏好；共享记忆只能由管理员编辑或经批准的提案写入；主体可以查看、更正、删除和导出属于自己的记忆与会话；过去的会话按需检索而不是整体塞进上下文。

**包含：**

- 私有记忆按 `workspace + agent + 调用主体` 隔离。主体用 M1 已有的 `CallerIdentity`；`CallerType` 现在是 `user` 与 `service_account`，§4.5 的终端用户身份落地时增加第三种，这张表的形状不变。**这是本路线图唯一的产品假设，见第 9 节。**
- 三种工作空间记忆策略（§14.1）：关闭自动记忆、全部候选待批、低风险经规则检查后自动写入。
- 共享记忆：只来自管理员直接编辑或经明确批准的提案；原始用户消息不默认进入。
- 记忆进入 A 阶段的「记忆」段，按相关度裁剪。
- PostgreSQL 全文搜索的会话检索，按需注入而不是全量进上下文。
- 主体自助的查看、更正、删除与导出（§4.6 矩阵中「本人」那一行）。
- 删除主体时私有记忆、会话、文件与身份信息的可追溯抹除流程（§344）。

**明确不包含：** 知识图谱、向量记忆、多个外部记忆提供方（§616 明确排除）。

**出口检查：**

- A 主体的私有记忆不出现在 B 主体的同 Agent 会话里。
- 策略设为「全部待批」时没有任何记忆绕过审批写入。
- 原始用户消息不会自动成为共享记忆。
- 记忆段超预算时先移除低相关记忆，不波及不可裁剪内容。
- 会话搜索只返回请求者有权读取的会话。
- 抹除流程执行后，私有记忆、会话、文件与身份信息都不可再检索，且抹除本身写审计。

## 8. 阶段五 M2E：一层并行子 Agent

**可运行成果：** 主 Agent 并行委派两个以上子 Agent，各自拥有独立 Run、Session、SessionWorkspace、事件、沙箱与费用记录；父子之间只通过 Artifact 授权传文件；整棵任务树与其重试链共享同一根预算；子 Agent 权限是父权限与委派策略的交集，且读不到未授权的父私有记忆。

**包含：**

- `depth` 最大为 1 的委派模型与 `delegation_scope`，覆盖工具、文件、网络、密钥、技能、记忆六个面。
- 子 Run 继承父 Run 的调用主体用于身份、审计与数据归属，但不继承父 Agent 的私有记忆内容。
- 父 Run 的 `waiting_external` + `wait_kind=child_runs` + `wait_policy=all|any` + 子 Run ID 集合 + `wait_deadline_at`；进入等待时释放 WorkerLease 并销毁 SandboxReservation 与 SandboxInstance。
- `any` 在首个子 Run 成功后唤醒并默认取消其余；全部子 Run 终止而无成功结果时父 Run 仍回 `queued` 并收到结构化失败摘要。
- 子结果的幂等投递与重试；父 Run 暂时不可用时结果保留。
- 取消父 Run 时级联取消仍在运行或等待的子 Run。
- 整棵树接入 `budget_root_run_id`，含最大并行子 Agent 数。
- 控制台的最小任务树视图（完整任务树是 M3）。

**出口检查：**

- 两个子 Run 真正并行执行，各自持有独立沙箱与独立 SessionWorkspace。
- 子 Agent 不能创建孙 Agent。
- 子 Agent 使用超出交集的工具、网络、密钥、技能或记忆权限时被执行层拒绝。
- 子 Agent 读不到未授权的父私有记忆（§27.2.3）。
- 父子不共享可写目录；文件只经 Artifact 授权传递。
- 整棵树的 Token、费用、执行时间、工具次数与重试次数累计在同一根预算，创建子 Run 不重置任何一项。
- 父 Run 暂时不可用时子结果不丢，恢复后按幂等键投递一次。

## 9. 本路线图做的一个产品假设

§14.1 说私有记忆按 `workspace + agent + end_user` 隔离，而 `end_user` 在代码里不存在——这是 `2026-08-16-end-user-entry.md` 记录的空缺，它背后有四个只有产品能回答的问题。

本路线图假设：**记忆的主体用 M1 已有的 `CallerIdentity`，而不是等待终端用户身份落地。**

理由：记忆的价值不取决于主体来自哪套身份体系，而 `CallerType` 本身就是为增加主体种类留的扩展点——终端用户落地时加第三个枚举值，记忆表的形状不变。真正取决于 §4.5 那四个答案的是**终端用户聊天入口**本身，而它按 §26 属于 M3，不在 M2 范围内。

如果这个假设不成立，两条退路都不改动其他阶段：

- 先回答 §4.5 的四个问题并在 M2 内交付 `EndUser` 类型，M2 多一个阶段。
- 把记忆推到 M3，第七、八节对调，§27.2.3 随之落到子 Agent 阶段之后。

除此之外，本路线图不替产品做任何未决决定。

## 10. 0.2 发布关口

只有以下条件同时满足才能标记 `0.2 Agent Preview`：

- 五个阶段的出口检查全部通过。
- 产品设计 §27.2 的 9 个场景全部有自动检查，或有证据的人工验收记录。
  **状态（2026-08-20）：七条完全满足，一条刚补齐，一条有条件。**

  | # | 场景 | 证据 |
  |---|---|---|
  | 1 | Goal 三种判断 + 可恢复暂停 | `test_goal.py`、`test_worker_goal.py`、`test_external_wait.py`、`test_budget_expansion.py` |
  | 2 | 并行子 Agent、Artifact 传文件、树**与重试链**共享根预算 | `test_child_runs.py`（含 `test_retrying_a_child_keeps_it_on_the_trees_budget`，本次补） |
  | 3 | 子 Agent 读不到父私有记忆、不能扩大权限 | `test_child_runs.py`、`test_delegation.py`、`test_child_waits.py` |
  | 4 | 两类记忆按策略写入 | `test_memory_write.py` |
  | 5 | 技能提案到不可变版本 | `integration/skills/`、`skills.spec.ts` |
  | 6 | 审批队列、终端用户不能批治理、参数变化失效、ServiceAccount 限制 | 前三项 `test_approvals.py` / `test_approval.py`；第四项见下 |
  | 7 | 压缩保留原始引用、装不下则暂停 | `test_context_budget.py` |
  | 8 | egress-proxy 唯一出站、四层交集 | `unit/outbound/`（5 条架构测试）、`integration/egress/` |
  | 9 | 两次权限检查、schema 超预算暂停 | `test_mcp_tools.py` |

  **场景 6 的第四句成立，但成立的原因是错的，不能就这样勾掉。**
  「无 EndUser 的 ServiceAccount Run 只能使用预授权或治理审批」今天是真的——
  但不是因为 ServiceAccount 被限制住了，而是因为**谁都拿不到用户确认**：
  `USER_CONFIRMATION` 在整个 `src` 里只有四处引用，全是枚举定义和消费端，
  `tool_answers.py` 只产生 `GOVERNANCE_APPROVAL`。M2C 的验收记录第 5 节已经
  写明这一点。等 M3 的终端用户入口把生产者接上，这一句才第一次被真正检验。
  **刻意没有为它补测试**：那个状态今天没有真实到达路径，要伪造一行审批记录
  才能让它发生，那样这一格会变绿而掩盖真问题。
- 全新 Linux Docker 主机可按文档启动，完成一次多轮 Goal 任务与一次并行子 Agent 委派。
- `egress-proxy` 是唯一出站路径，架构测试证明没有旁路。
- 本里程碑新增的每一种暂停——`limit`、`context_overflow`、`tool_budget_exceeded`、`external_timeout`、审批相关——都演示过恢复，且恢复不重置累计安全阀。
  **状态（2026-08-20）：五种全部有恢复测试，均断言累计值不清零。**
  | 暂停 | 恢复测试 |
  |---|---|
  | `limit` | `test_budget_expansion.py::test_the_run_goes_back_to_work_and_spends_the_room_it_was_given` |
  | `tool_budget_exceeded` | `test_mcp_tools.py::test_resuming_measures_again_and_charges_nothing_for_the_first_try` |
  | `context_overflow` | `test_context_budget.py::test_a_context_overflow_pause_can_be_recovered_and_keeps_what_it_spent` |
  | `external_timeout` | `test_child_waits.py::test_an_external_timeout_pause_recovers_and_the_tree_keeps_its_counters` |
  | 审批相关 | `test_approvals.py::test_an_expired_approval_pause_recovers_and_keeps_what_it_spent` |
  前两条本来就有；后三条是补的——原先只测到「暂停发生」，没测到「恢复且不清零」。
  `context_overflow` 是其中最别扭的一个：它是唯一没花掉模型调用就到达的暂停，
  所以「恢复不重置」要守的那个计数器本身就没动过。仍然断言了，因为这条规矩
  说的是平台永远不重开一个 Run 的账，而不是那个数字碰巧好看。
- §24.1 的 0.1 门槛在 0.2 上重跑不退化；新增的轮次与压缩没有引入新的性能回退。
  **状态（2026-08-19）：回归这一半已答，绝对门槛这一半没答。** 同机 A/B
  （0.1 `5ad0e00` 对 0.2 `5e28996`，同一份基准工具、两端 `down -v` 起）十项
  无有意义退化，`create_run`、`run_event`、`next_run` 全部保持——记录见
  `docs/superpowers/verification/2026-08-19-m2e-child-agents.md` §8。
  但那台机器是 macOS、VM 只有 7.8 GiB，工具自报 `shape_ok: false`，所以
  **绝对门槛仍需在 Linux 8 vCPU / 16 GiB 参考主机上重跑**才算数。
  另有一项待查：`workspace_small` 在 0.1 和 0.2 两端都超门槛（2678ms /
  2772ms 对 1000ms），差值仅 3.5%，不是 M2 引入的；是真超标还是环境所致，
  要在参考主机上才能分辨。
- 项目描述从「单 Agent 安全运行骨架」升级为「轻量 Hermes 内核预览」，仍不宣传为企业交付闭环。终端用户 Web Chat、飞书适配器、OIDC、审计查询导出与完整任务树都还没有，它们是 M3。
  **状态（2026-08-20）：已改。** `pyproject.toml` 是唯一活的描述位；M1 的计划
  与规格文档里那句话是当时的记录，没有动。守着它的那条测试
  （`test_acceptance_records.py`）从钉 M1 的说法改成钉这一句，并加了一条
  「不许出现『企业』」。项目没有 README，所以「0.2 是什么、**不是**什么」
  写进了 `docs/development.md` 开头一节：五项 M3 的缺失逐条列出，外加
  HTTP/MCP 不可委派与 §24.1 未在参考主机认证两条。
  **注意顺序**：这一条是「标记 0.2」的条件之一，而关口尚未全部满足——
  合并后 `main` 上就写着「预览」，「预览」本身是诚实的，但真正可以宣布 0.2
  仍要等 Linux 参考主机那两条。
- 发布后按 §1265 评估 Goal 失控率、子 Agent 成功率、审批负担、记忆误写和技能提案质量，再决定 M3 范围。
