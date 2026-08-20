# M2A-2 上下文预算、裁剪与压缩实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**设计：** `docs/superpowers/specs/2026-08-17-m2a-goal-loop-design.md` §4.8–§4.9、§5。
**产品：** `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` §7.4.2。
**路线图：** `docs/superpowers/plans/2026-08-17-tiny-hermes-m2-roadmap.md` §4。
**前一半：** `docs/superpowers/plans/2026-08-17-m2a-goal-loop.md`（M2A-1，已完成）。

**目标：** M2A-1 让 Run 跑得更久，这一半让它跑得久还装得下。一次请求发出去之前，
平台先按声明的窗口算出这轮能发什么、裁掉什么、压缩什么；装不下的时候暂停而不是
悄悄删。

**顺序原则：** 第 1 步是纯函数，没有 I/O、没有数据库、没有 Worker；第 2–4 步是声明
与发布期校验，运行时行为不变；行为变化从第 5 步开始。每一步都能独立跑通。

**两条贯穿全篇的红线：**

- **规划器的数字永远是计划估算，不是用量。** `UsageQuality` 里没有 `estimated`
  是 0.1 的明确决定（`runs/ports/model.py:38`）。规划器决定「发什么」，计费仍然
  只来自响应。所以估算值不写进 `consumed_tokens`，不写进 checkpoint 的 `tokens`，
  也不以「token 数」的名义出现在任何面向用户的地方。
- **没有一条分支会让消息变得不可达。** 裁剪保留原始引用，压缩记录覆盖范围与
  原始 id，压缩失败用原文，原文装不下就 `paused(context_overflow)`。

---

## 1. 纯规划器

- [x] `packages/backend/tests/unit/runs/test_context_budget.py`：先写会失败的测试。
      覆盖：§7.4.2 表格的默认分段（七段，min/target/max 与是否可裁剪）；
      估算函数对同一段文本是**上界**而不是猜测；不可裁剪的最小合计超过窗口时
      规划器返回「装不下」而不是抛异常；宽松窗口下什么都不裁。
- [x] `packages/backend/src/tiny_hermes/runs/domain/context_budget.py`：
      - `SegmentName`（`safety_rules` / `personality` / `skill_summaries` /
        `memory` / `tool_schemas` / `old_tool_results` / `recent_history`）。
      - `SegmentBudget`（`min_tokens`、`target_tokens`、`max_tokens`、
        `trimmable`、`priority`）与 `DEFAULT_SEGMENTS`，数值照抄 §7.4.2 表格。
      - `ContextWindow`（`context_window`、`max_output_tokens`、
        `accounting`、`tokenizer`）与 `available_input_tokens`：`shared` 时要
        扣掉预留输出，`separate` 时不扣。**按端点声明算，不按 Provider 名字猜。**
      - `estimate_tokens(text, tokenizer)`：声明的 tokenizer 命中注册表就用它；
        没有命中就走保守的按字符上界 + headroom。返回值的类型名里带 `estimate`，
        让调用方读不成 usage。
      - `plan_context(...) -> ContextPlan`：纯函数，输入是消息、工具 schema、
        人格、安全规则和窗口，输出是要发的消息、被裁掉的东西和一份记录。
- [x] 无 I/O：不 import SQLAlchemy、httpx，也不调 `datetime.now`。
      同一份输入两次调用必须得到同一份输出——压缩摘要是结构化生成的，不是模型写的。

## 2. 固定裁剪顺序

- [x] 测试先行：旧工具大结果最先被裁；未命中技能摘要其次；低相关记忆第三；
      最近历史的结构化压缩最后。**工具调用与工具结果不能拆开**——裁掉一个
      `tool_result` 就必须连同它的 `tool_call` 一起处理，反过来也一样，
      否则发出去的是一个永远等不到回答的调用。
- [x] `memory` 段在本阶段被分配但恒为空（设计 §7）。写成「分配了、目前没有内容」
      而不是「不存在」：顺序里有它的位置，M2D 填进来时不需要改顺序。
- [x] 被裁掉的工具结果留下原始引用（`call_id` 与一句说明它有多大），
      不是直接消失。模型读到的是「这里曾经有一段 12 KB 的输出」，
      而不是一个语义上不存在的洞。

## 3. 结构化压缩

- [x] 测试先行：压缩后返回的计划里带覆盖范围 `(first_sequence, last_sequence)`
      与被覆盖的原始消息 id；原文仍在 `session_messages` 里；
      压缩不动最新一轮和当前用户请求。
- [x] 摘要是**结构化生成**的，不调模型：几轮、哪些角色、调了哪些工具、
      产出多少字节。确定性带来三件事——可以直接断言、重复计算不花钱、
      重放同一个 Run 得到同一份上下文。
- [x] 「压缩失败保留原文」这条在纯函数层就是可测的：压缩后仍然装不下时，
      规划器返回 `fits=False` 并把原文原样交回，由调用方决定暂停。
      规划器自己不做状态决定（与 §9 第一条风险同一条原则）。

## 4. 端点声明：`context_accounting` 与 `tokenizer`

- [x] `ModelEndpointSpec` 加 `context_accounting: Literal["shared","separate"]`
      （默认 `shared`，因为它是更保守的那一个）与 `tokenizer: str | None = None`。
- [x] `model_endpoints` 两个新列 + 迁移 `20260817_0013_context_budget`。
      同一次迁移里把 `run_events.event_type` 的 CHECK 换成含
      `context_trimmed` / `context_compacted` 的新版本——两处都是「新增枚举值
      要带迁移」，合成一次比分两次少一次停机窗口。
- [x] API schema 与控制台端点表单加这两个字段；控制台已有的端点测试补一条。
- [x] **平台不自带任何已验证 tokenizer。** 注册表现在是空的，
      每个端点都走保守上界。这不是缺口，是 §4.8 写明的默认；
      注册表存在是为了以后加一个已验证的实现时不用动规划器。

## 5. `AgentSpec.context_budget`（加宽）

- [x] 测试先行，先证明加宽不改内容哈希：不声明 `context_budget` 的 spec 仍然
      哈希成 `DETERMINISTIC_HASH`，`schema_version` 仍是 1，normalized document
      里根本没有这个键。和 `tools`、`completion` 两次加宽同一条做法。
- [x] `ContextBudget`：逐段可选覆盖，只能在平台硬上限内。
      工作空间管理员和 Agent 开发者能调的就是这个。

## 6. 发布期 `context_budget_unsatisfied`

- [x] 测试先行：把端点窗口调到只装得下不可裁剪内容 → 发布被拒，
      错误里带**逐段缩放建议**；建议**不会静默生效**，作者改完再发布才通过。
- [x] `AgentCatalog._check_endpoint` 是现成的缝：它已经读了端点。
      在同一个地方判断「这份分段配置能不能装进这个端点的窗口」。
      只对端点型 policy 生效——stand-in 没有窗口可言。
- [x] 建议里带的是每段的具体数字，不是一句「太大了」。
      作者看到 `40` 却只被告知「无效」时无从知道 `20` 才是答案——
      `RoundCeilingExceeded` 已经立过这个规矩，这里照办。

## 7. 接到 Worker 上

- [x] `ExecutionContext` 带上 `window: ContextWindow | None`。
      端点型 policy 从 `model_endpoints` 行读；stand-in 是 `None`，
      意思是「没有声明窗口」，于是不裁剪——这不是特例，是事实。
- [x] `worker.py:_request()` 改成先规划再构造 `ModelRequest`。这是设计
      里点名的那道缝，M2A-1 特意没有碰它。
- [x] 规划器说装不下时：**不调 Provider**，
      `SliceDecision(SAFE_PAUSE_REACHED, PauseReason.CONTEXT_OVERFLOW)`，
      按 `_open_sandbox` 失败那条路径记录（`_no_round()`，`model_calls=0`）。
      一次没发生的调用不该记账。
- [x] 新事件类型 `context_trimmed` 与 `context_compacted` 写进 `RunEventType`，
      并在 `event_type_for` 的注释里说明它们和 `goal_verdict` 一样不是从信号派生的。
- [x] 集成测试（`tests/integration/runs/test_context_budget.py`）：
      建一个窗口很小的真实端点行 + 端点型 policy 的 Agent，模型仍然是替身；
      长对话 → 被裁剪并留下事件；更长 → 被压缩且事件里有覆盖范围与原始 id；
      不可压缩的输入 → `paused(context_overflow)` 且这一轮没有模型调用。

## 8. 控制台

- [x] Run 详情时间线认识这两个事件，并说出人话：裁了什么、压了哪几条。
      i18n 键写进 `zh-CN`，和 `goal_verdict` 同一处。
- [x] `paused(context_overflow)` 的状态说明：这一条和 `paused(limit)` 一样，
      读的人需要知道自己是不是那个卡住它的人。`explain.ts` 已经有这个形状。
- [x] 前端单测覆盖新的说明文案，`explain.test.ts` 加一条。

## 9. 验收与记录

- [x] 后端、ruff、pyright、vitest、tsc、eslint、e2e 全跑。
- [x] 路线图两条出口检查：
      - 端点窗口缩到只装得下不可裁剪内容 → 发布被拒并给出逐段建议；
        同样的输入在运行时 → `paused(context_overflow)`。
      - 压缩之后能从原始引用取回被覆盖的消息范围。
- [x] 把 M2A-2 这一半写进
      `docs/superpowers/verification/2026-08-17-m2a-goal-loop.md`，
      并把 §6「未声明」里的 M2A-2 一条删掉——那条到这里为止不再成立。
