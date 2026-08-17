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

- [ ] 单元测试：平台上限调高后 `AgentLimits(max_model_calls=40)` 通过校验；
      调低到 10 时同一个值被拒。
- [ ] `AgentLimits.max_model_calls` 的字面 `le=20` 改为对平台设置的校验，
      默认仍是 20。已发布 spec 不因此失效。
- [ ] 集成测试：撞上限的 Run 进入 `paused(limit)` 并写 `run_limit_reached`；
      有权限主体扩大预算后继续，`consumed_*` 全部不重置。

## 7. `wait`：`waiting_external` 第一次有生产者

- [ ] 集成测试：判定 `wait` 的 Run 进入 `waiting_external`，`wait_kind="timer"`，
      带 `wait_deadline_at`；断言租约已释放、沙箱已销毁。
- [ ] Scheduler 到期把它重新排进 `queued`；超期无唤醒 → `paused(external_timeout)`。
      Scheduler 那条从未被触发的等待扫描第一次有输入，删掉 `scheduler.py:281`
      那句「阶段 2B 从不产生 waiting_external」的注释。

## 8. 让外面看得见

- [ ] 新事件类型 `goal_verdict`；`RunSnapshot` 增加当前轮次与最近一次判定，
      `RunResponse` 随之扩展。
- [ ] 控制台 Run Detail 显示轮次与判定理由；`waiting_external` 与
      `paused(limit)` 各有明确文案，不是一个通用的「等待中」。
- [ ] `tests/e2e/console.spec.ts` 增加一条：多轮任务在控制台上可以看出它为什么还没结束。

## 9. 阶段出口（对应设计 §2）

- [ ] 需要三轮以上工具调用的任务由判断器判 `done` 结束。
- [ ] 模型判 `done` 但校验命令失败时服务端不接受。
- [ ] 轮数上限触发后可恢复，累计值不重置。
- [ ] `wait` 的 Run 释放租约、销毁沙箱，到期被唤醒；超期进入 `paused(external_timeout)`。
- [ ] `uv run pytest`、静态检查、前端单测与 e2e 全部通过。
