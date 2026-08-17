# M2A-1 Goal 循环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**设计：** `docs/superpowers/specs/2026-08-17-m2a-goal-loop-design.md` §4.1–§4.7。
**路线图：** `docs/superpowers/plans/2026-08-17-tiny-hermes-m2-roadmap.md` §4。

**目标：** 让 Run 的结束由平台判定，而不是由模型宣布。

**顺序原则：** 第 3 步之前不改变任何可观察行为——它是一次纯重构，把
`decide_after_round` 的输入从 Provider 的 `StopReason` 换成平台的 `GoalVerdict`，
0.1 的全部测试必须原样通过。行为变化从第 4 步开始，每一步都能独立跑通。

---

## 1. 纯判断器

- [x] `packages/backend/tests/unit/runs/test_goal.py`：先写会失败的测试。覆盖
      无完成条件时 `COMPLETED` → `done`（0.1 行为）；声明了条件但校验未通过时
      `COMPLETED` → `continue` 且带未满足项与指令；校验无法执行 → `undecidable`；
      `failed` 原样透传且不看证据。
- [x] `packages/backend/src/tiny_hermes/runs/domain/goal.py`：`GoalProposal`、
      `GoalEvidence`、`GoalVerdict`（`done` / `continue` / `wait` / `failed` /
      `undecidable`）与纯函数 `judge()`。无 I/O，不引用 SQLAlchemy、httpx 或
      `datetime.now`。
- [x] **判断器只回答「目标达成了吗」，不回答「Run 接下来是什么状态」。** 取消、
      暂停和预算的优先级留在 `decide_after_round`——它已经有这个顺序也已经有测试，
      判断器复制一份就会出现第二个决定 Run 状态的地方（设计 §9 的第一条风险）。
      第 3 步在 `test_slice_policy.py` 里补一条：`done` 判定仍然输给取消、暂停和预算。

## 2. AgentSpec 的完成条件（加宽）

- [x] `test_completion_condition.py`：先证明加宽不改内容哈希——不声明 `completion`
      的 spec 仍然哈希成 `DETERMINISTIC_HASH`，`schema_version` 仍是 1，
      normalized document 里根本没有这个键。
- [x] `agents/domain/models.py`：`CompletionCondition`（`expected_artifacts`、
      `verification_command`、`constraints`、`stop_conditions`），
      `AgentSpec.completion: CompletionCondition | None = None`，为空时从
      normalized document 中删除。
- [x] 发布期校验，四条，都是「平台跑不了的检查」这一条原则的展开：
      - `verification_command` 非空时必须绑定 **`shell.exec`**。计划原文写的是
        `command.run`，但代码里没有这个工具名——校验命令走的就是 `shell.exec`
        那条沙箱路径，0.1 的宿主机回退禁令因此原样覆盖。
      - `expected_artifacts` 非空时至少要绑定一个工具：`worker.py:249` 只在
        `spec.tools` 非空时开沙箱，一个工具都不绑的 Agent 没有任何东西能写出
        那个文件，这个条件永远不可能满足。
      - 只写 `constraints`（或什么都不写）的完成条件被拒绝：`goal.py` 对「声明了
        但没有任何检查」的回答是 `continue`，这样的 Agent 会一直跑到轮数上限。
      - `stop_conditions.max_rounds` 不得超过 `limits.max_model_calls`：预算会
        先停下这个 Run，而作者读到的是自己声明的那个上限。
- [x] `expected_artifacts` 复用 `normalize_workspace_path`：NFC、相对路径、
      拒绝绝对路径与 `..`，与清单和文件工具是同一套写法。两条路径归一化后相同
      也拒绝——一个只能回答一次的检查不该被数两遍。

## 3. 把 `StopReason` 换成 `GoalVerdict`（纯重构）

- [x] `slice_policy.py`：`RoundOutcome.stop_reason` → `RoundOutcome.verdict`。
      `decide_after_round` 的优先级顺序不变（取消 > 暂停 > 预算 > 判定 > 兼容超时 > 时间片）。
      `undecidable` → `paused(operator)`、`wait` → `EXTERNAL_WAIT_STARTED` 这两条
      映射一并写好；它们的生产者分别在第 4 步和第 7 步才出现，所以此刻不可达。
- [x] `worker.py:_execute_slice`：调用 `judge()` 得到 verdict 再交给
      `decide_after_round`。此时证据恒为「无完成条件」，判定与 0.1 等价：
      `COMPLETED`→`done`、`FAILED`→`failed`、`CONTINUE`/`TOOL_CALL`→`continue`。
- [x] `test_slice_policy.py` 补一条：`done` 判定仍然输给取消、暂停和预算。
- [x] 出口：单测 718 全绿，ruff 与 pyright 干净，`tests/e2e/console.spec.ts` 不改一行。

## 4. 校验命令：让虚假的 `done` 站不住

- [x] 集成测试 `tests/integration/runs/test_worker_goal.py`（8 条）：必然失败的
      `verification_command` + 第一轮就报 `COMPLETED` → Run 没有结束；校验通过 →
      结束；第一次失败第二次通过 → 结束且校验跑了两次；不声明完成条件的 Agent
      一条命令都不执行（0.1 行为）；缺失的 `expected_artifacts` 是未通过的检查。
      **事件断言挪到第 8 步**：`run_events.event_type` 上有一条从枚举生成的
      CHECK 约束，新增事件类型要带迁移，和第 8 步的 `RunSnapshot` 字段合成一次。
- [x] Worker 在收到 `done` 提议且存在完成条件时，于当前沙箱执行校验命令，
      结果作为 `GoalEvidence` 交给判断器。走的就是 `shell.exec` 那条
      `sandbox.execute` 路径，不新开执行入口——宿主机回退禁令因此原样覆盖。
- [x] `expected_artifacts` 用沙箱里的 `test -e` 判定存在性，而不是冻结扫描：
      判定必须发生在 `decide_after_round` 之前，而冻结扫描发生在 `_checkpoint_round`
      里、也就是决定之后；为了几个路径把整棵树扫一遍也不划算。
- [x] 校验的副作用不提交。检查是观察，写入是 Agent 自己的轮次干的事；一次通过的
      检查顺手往工作区里加文件，会让记录本身变得不对。
- [x] 校验无法执行（控制器拒绝、命令超时被杀）→ `paused(operator)`，
      不静默接受也不静默否决。超时算「没有回答」而不是「没通过」，
      否则一个跑得慢的检查会把 Run 一路循环到预算耗尽。

## 5. `continue` 的下一轮指令

- [x] 集成测试（2 条）：第 4 步那个 Run 的下一轮请求里恰好一条平台指令，它出现
      `pytest -q`、不出现任务原文；抄本里那一轮存成 `role="user"` 但带作者标记，
      与人类那一条区分得开。
- [x] 判断器生成指令文本（`goal.py:_instruction_for`），Worker 作为 `role="user"`
      的 `CanonicalMessage` 追加。指令只说明哪几项没通过——任务本身已经在对话里，
      复述它是在花上下文买模型已经能读到的东西。
- [x] 作者标记是 `CanonicalMessage.author`，加宽方式与第 2 步同：默认 `None`、
      为空时不进 document，所以本切片之前写下的每一行抄本字节不变
      （`test_platform_messages.py` 用人类那一条的 document 逐字节钉住）。
      读回时只认识 `"platform"`，其它值一律当人类——这个方向是安全的那个，
      把一条读不懂的记录说成平台的，等于替别人说话。

## 6. 轮数上限从常量变成平台设置

- [x] 单元测试：`test_round_ceiling.py`，平台上限调高后 40 存得下、调低到 10 时
      同一个值被拒，而且昨天在 40 下存好的草稿今天也发布不出去——草稿是拿当天的
      上限量过的，发布是错误还便宜的最后一刻，所以两处都查。
- [x] 上限搬到 `PlatformCeilings`（`agents/domain/models.py`），字段只留 `ge=1`。
      `le=` 不能留在字段上：已发布的 AgentVersion 是一份文档，Run 每次启动都要把它
      解析回 `AgentLimits`，字段上的上限意味着管理员调低设置就让一批线上 Agent
      的文档解析失败——一次配置改动打断了正在跑的活，设计 §4.7 明确排除这件事。
      上限只在**写入**处查（存草稿、发布），读回永不查。
- [x] §12.3 的「默认」那一半仍是字段字面量 20，没有跟着设置走：默认决定了
      省略 `limits` 的 spec 规范化成什么，跟着配置走就会让同一份提交的 spec
      在两套安装上算出不同的 content hash。设置项是 `agent_max_model_calls`。
- [x] 集成测试：`test_budget_expansion.py`，撞上限的 Run 进入 `paused(limit)`、
      写 `run_limit_reached`、`available_actions` 里没有 `resume`；扩大预算后
      `resume` 自己回来了——扩容不碰 Run 状态，它改的是
      `RunStateMachine.budget_allows_execution` 读到的那个数。
- [x] 扩容是 `POST /api/v1/runs/{id}/budget`，只给 `BUDGET_HOLDERS =
      {Role.WORKSPACE_ADMIN}`：开发者能启停 Run，但决定这个工作区要比约定的花得更多
      是管理员的事。带 `expected_state_version`，只升不降，审计写
      `run.budget_widened`（拒绝写 `run.budget_widen_denied`）。
- [x] 命令里根本不带 `consumed_*`：计数器不是这个操作的事，一个能顺手清零的扩容
      就是重花同一笔预算的后门，正是 §12.3 最后一句要堵的。暂不写新的 run event，
      `run_events.event_type` 的 CHECK 是从枚举生成的，新事件要一支 Alembic
      迁移——留给第 8 步的 `goal_verdict` 一起做。

## 7. `wait`：`waiting_external` 第一次有生产者

- [x] 生产者是一个绑定工具 `platform.wait`。工具调用是模型向平台开口的唯一通道，
      而 `authorize()` 的产物是 `SandboxCommand`——平台工具没有命令可交，所以
      `PLATFORM_TOOLS` 在授权**之前**分叉，由 Worker 自己应答，永不下发给 Controller。
- [x] 时长在纯 registry 里校验：`1..86_400` 秒，无默认值（缺参即拒），
      `isinstance(True, int)` 为真所以布尔先于范围被排除。不合法的调用是
      `INVALID_ARGUMENTS` 拒绝，不是一次崩溃的轮次。
- [x] 只绑定平台工具的 Agent 不开沙箱。没有东西要放进容器，而为它开一个，
      就等于让一个即将等待的 Run 攥着实例去等——正是 §12.3 承诺不会发生的事。
- [x] 截止时刻的算术归 store：`SliceDecision`/`RecordSliceCommand` 只带
      `wait_seconds`（一个时长），`record_slice` 用它给这次事务盖章的同一个 `now`
      算出 `wait_deadline_at`，Scheduler 扫到的截止时刻不可能早于宣布它的那一行。
- [x] 集成测试（8 个）：判定 `wait` 的 Run 进入 `waiting_external`，`wait_kind="timer"`，
      截止时刻来自模型请求的时长；租约已释放、`sandbox_reservations` 一行没有；
      未绑定该工具的 Agent 发同样的调用只会被拒，永远到不了 `waiting_external`。
- [x] **到期意味着什么，取决于截止时刻归谁。** 定时器的截止时刻是平台自己的，
      到点即唤醒 → 回 `queued`；等待外部的种类（M2C 的审批、M2E 的子 Run）到点
      才是「没人应答」→ `paused(external_timeout)`。所以扫描按 `wait_kind` 分叉，
      `scheduler.py` 那句「阶段 2B 从不产生 waiting_external」的注释已删除，
      方法改名 `_settle_due_waits`，唤醒的 Run 在事务提交后才 `_announce`。
- [x] 不新增 run event 类型，因而不需要 Alembic 迁移（迁移留给第 8 步的
      `goal_verdict` 一并做）。

## 8. 让外面看得见

- [x] 新事件类型 `goal_verdict`，带 Alembic 迁移（`20260817_0012`）：`event_type`
      的 CHECK 是从 `RunEventType` 机械推出来的，多一个名字就得多一次迁移。
- [x] **一轮判定写在 checkpoint 里，不是新开一列。** `round`、`goal_outcome`、
      `goal_unmet` 与 `failure`、`usage_quality` 是同一类东西——都在描述某一轮，
      单独开列也得跟 checkpoint 保持一致，那不如只有一处。`RunSnapshot` 从
      checkpoint 读出来，`RunResponse` 以一个 `goal` 对象暴露：三者是一件事，
      让调用方分别判断三个键在不在，多出来的分支换不到任何信息。
- [x] **补上一个结构性的洞：一轮判定「继续」的 Run 原来什么都不写。**
      `record_slice` 在 `command.signal is None` 时提前返回——那一轮不改状态，
      没有转换可以挂事件，于是连 `command.events` 一起丢掉了。而那恰好就是
      时间线该有话说的 Run：它干了好几轮，时间线上一片空白。现在这条路径单独
      写事件。5 条集成测试，其中一条就是「每一轮都在时间线上留下判定」。
- [x] 轮次用 `_round_index(context) = consumed_model_calls + 1`，模型收到的
      `round_index` 与人读到的 `goal.round` 是同一个数，跨 slice 也不会各算各的。
      被回滚的那一轮不带判定——判定没有生效，这是实话。
- [x] 控制台 Run Detail 显示轮次、判定与未通过的检查；`waiting_external`（分定时器
      与外部答复两种）与 `paused(limit)` 各有一条说明文案。措辞逻辑抽到
      `apps/web/src/runs/explain.ts`，因为「这个状态该说什么」是可以单测的，
      而组件不是。
- [x] 顺手补上第 7 步漏掉的一半：`IMPLEMENTED_TOOLS` 少了 `platform.wait`，
      `MODEL_SCENARIOS` 少了三个后端已实现的场景。平台跑得了而控制台叫不出名字的
      场景，等于从界面上根本发不起——`waiting_external` 一度就是这样没法从 UI 走到。
- [x] `tests/e2e/console.spec.ts` 增加一条：`wait_once` 的 Run 停在
      `waiting_external`，页面上写着第几轮、判定是「等待」、等的是定时器；
      被唤醒后完成时轮次是 2 而不是重新从 1 数，两轮判定都还留在时间线上。

## 9. 阶段出口（对应设计 §2）

- [ ] 需要三轮以上工具调用的任务由判断器判 `done` 结束。
- [ ] 模型判 `done` 但校验命令失败时服务端不接受。
- [ ] 轮数上限触发后可恢复，累计值不重置。
- [ ] `wait` 的 Run 释放租约、销毁沙箱，到期被唤醒；超期进入 `paused(external_timeout)`。
- [ ] `uv run pytest`、静态检查、前端单测与 e2e 全部通过。
