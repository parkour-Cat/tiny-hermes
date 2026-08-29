# 上下文压缩：模型摘要与比例触发 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让旧会话被压成一份**模型生成的语义摘要**（生成一次、持久化、之后每轮读存下来的那份），并把压缩从「装不下才动」改成「到达比例就动」。

**Architecture:** `context_budget.py` 保持纯函数、无 I/O、每轮重算——摘要文本作为**输入**传进去，不在里面生成。Worker 在发现本轮需要一份尚不存在的摘要时，调一次辅助模型、落库，再重新规划一次；失败则沿用领域层已经算出的确定性结构摘要。

**Tech Stack:** Python 3.12、SQLAlchemy 2 async、Alembic、pytest、PostgreSQL。

## 任务顺序不可调换，理由写在这里

**模型摘要必须先落地，比例触发最后做。**

反过来做会让产品变差：比例触发的唯一作用是让压缩**更早、更频繁**地发生。在摘要还是
确定性结构清单（「这里曾有 38 条消息，19 条 assistant」）的时候提前触发，等于更早地把
真实内容换成一句什么都没说的话。先把摘要做好，再让它更早发生，每一步都是改进。

## Global Constraints

- 产品事实来源：`docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.7 §7.4.2。冲突以它为准。
- **测试先写，跑它，看它红，再实现。提交分开：先 test 再 impl。**
- 注释和 docstring 解释「为什么」，不解释「做了什么」。
- **一条注释不得声称代码没有的保护。**
- **断言按 id 找行，不要按下标。**
- **已发布 AgentVersion 的内容哈希不得变化。** 新增的可选字段**不写就不能带那个键**。
  Task 6 有一步专门验证这件事，不得跳过。
- `context_budget.py` **必须保持无 I/O**。它的模块 docstring 明写 "it has no I/O at all"，
  而重放确定性依赖这一点。摘要文本只能作为参数传入。
- 迁移 head 以 `git log` 里最新的 `migrations/versions/` 为准，本计划新增一个。
- 跑测试前两行必须分开写：
  ```
  export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
  export DATABASE_URL="$TEST_DATABASE_URL"
  ```
- **永远只跑一个 pytest。**
- 不改 `pyproject.toml`。
- 部署用 `deploy/compose/redeploy.sh`，不要手搓 docker compose 命令。

---

### Task 1: 存下来的摘要有个地方放

**Files:**
- Create: `migrations/versions/<next>_session_compaction.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/tables.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/ports/store.py`
- Test: `packages/backend/tests/integration/runs/test_compaction_store.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  ```python
  @dataclass(frozen=True)
  class StoredSummary:
      session_id: UUID
      first_sequence: int
      last_sequence: int
      text: str
      source: str          # "model" | "structural"
      endpoint_id: UUID | None
      model: str | None

  # SqlRunStore
  async def latest_summary(self, session_id: UUID) -> StoredSummary | None
  async def save_summary(self, summary: StoredSummary, *, workspace_id: UUID) -> None
  ```

表 `session_compactions`：`id`、`session_id`（索引）、`workspace_id`、`first_sequence`、
`last_sequence`、`summary`（Text）、`source`（String(16)）、`endpoint_id`（nullable UUID）、
`model`（nullable String(200)）、`created_at`。

**每个 Session 只保留最新的一份**，而不是每次压缩存一行历史：§7.4.2 要求后续压缩
**更新**上一份摘要而不是从头重写，所以「上一份」是唯一被读的东西。`save_summary`
按 `session_id` upsert。旧摘要不需要保留——它覆盖的原文一条都没删，真要追溯从
`CONTEXT_COMPACTED` 事件走。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_compaction_store.py
"""压缩摘要要存得住、读得回，而且只保留最新的一份。

只留最新一份是有意的：§7.4.2 让后续压缩更新上一份摘要，所以被读的永远只有
它。原文一条都没删，追溯走 CONTEXT_COMPACTED 事件。
"""

from uuid import uuid4

from tiny_hermes.runs.ports.store import StoredSummary


async def test_a_summary_comes_back_as_it_went_in(store, seeded_session) -> None:
    session_id, workspace_id = seeded_session
    written = StoredSummary(
        session_id=session_id,
        first_sequence=1,
        last_sequence=40,
        text="用户在排查一条飞书图片管道的故障。",
        source="model",
        endpoint_id=None,
        model="deepseek-v4-flash",
    )

    await store.save_summary(written, workspace_id=workspace_id)

    assert await store.latest_summary(session_id) == written


async def test_a_second_summary_replaces_the_first(store, seeded_session) -> None:
    session_id, workspace_id = seeded_session
    for last, text in ((40, "第一份"), (72, "第二份")):
        await store.save_summary(
            StoredSummary(session_id, 1, last, text, "model", None, "m"),
            workspace_id=workspace_id,
        )

    found = await store.latest_summary(session_id)

    assert found is not None
    assert found.last_sequence == 72
    assert found.text == "第二份"


async def test_a_session_with_no_compaction_has_no_summary(store, seeded_session) -> None:
    session_id, _ = seeded_session

    assert await store.latest_summary(session_id) is None
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_compaction_store.py -q
```
Expected: FAIL — `ImportError: cannot import name 'StoredSummary'`

- [ ] **Step 3: 实现**

迁移 docstring 要写明为什么每个 Session 只留一行（上面 §Task 1 那段理由），
不要只写「创建表」。表定义、`StoredSummary`、两个 store 方法照 Interfaces 写；
`save_summary` 用 `insert(...).on_conflict_do_update(index_elements=["session_id"])`，
并在 `session_id` 上建唯一约束——**唯一约束是「只留最新一份」这句话的执行者**，
没有它那句话只是注释。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_compaction_store.py -q
```
Expected: PASS，3 条。

- [ ] **Step 5: 提交**

```bash
git add packages/backend/tests/integration/runs/test_compaction_store.py
git commit -m "test(runs): 压缩摘要存得住、读得回、只留最新一份"
git add migrations/versions packages/backend/src/tiny_hermes/runs/infrastructure/tables.py \
        packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py \
        packages/backend/src/tiny_hermes/runs/ports/store.py
git commit -m "feat(runs): 压缩摘要有个地方放"
```

---

### Task 2: 规划器接受一份存下来的摘要

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/domain/context_budget.py`
- Test: `packages/backend/tests/unit/runs/test_stored_summary.py`

**Interfaces:**
- Consumes: 无（领域层不认识 Task 1 的存储）。
- Produces:
  ```python
  # plan_context 新增关键字参数
  stored_summary: str | None = None

  # CompactionRecord 新增字段（最后一个，有默认值）
  source: str = "structural"     # "model" | "structural"
  ```
  `plan_context` 在需要压缩时：`stored_summary` 非空则用它当摘要文本、
  `record.source = "model"`；为空则退回 `_summarize` 生成的结构摘要、
  `record.source = "structural"`。

**领域层不判断摘要新不新。** 它拿到什么就用什么，覆盖范围由调用方保证——
`context_budget.py` 没有 I/O，无从知道那份摘要覆盖到哪一条，硬要它判断就得把
存储语义搬进纯函数里。范围的正确性是 Task 3 的责任，并在那里测。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/runs/test_stored_summary.py
"""压缩用传进来的那份摘要，而不是每轮现编一份。

现编会同时坏两件事：同一个 Run 重放得到不同的上下文，以及每轮多付一次模型
调用。所以摘要是输入，不是这一层的产物。
"""


def test_a_stored_summary_is_what_the_model_sees(long_history) -> None:
    plan = _plan_with(long_history, stored_summary="用户在排查一条图片管道的故障。")

    assert plan.compacted is not None
    text = _first_text(plan.messages)
    assert "用户在排查一条图片管道的故障。" in text
    assert plan.compacted.source == "model"


def test_without_one_it_falls_back_to_the_structural_summary(long_history) -> None:
    plan = _plan_with(long_history, stored_summary=None)

    assert plan.compacted is not None
    text = _first_text(plan.messages)
    assert "compacted by the platform" in text
    assert plan.compacted.source == "structural"


def test_the_stored_summary_is_not_used_when_nothing_is_compacted(short_history) -> None:
    plan = _plan_with(short_history, stored_summary="不该出现")

    assert plan.compacted is None
    assert "不该出现" not in "".join(_all_text(plan.messages))
```

> `_plan_with`、`_first_text`、`_all_text`、`long_history`、`short_history` 是本任务
> 要写的辅助与夹具，放在同一个文件里；照 `tests/unit/runs/test_context_budget.py`
> 现有的构造方式写，不要另起一套。`long_history` 必须长到在默认预算下真的触发压缩。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/runs/test_stored_summary.py -q
```
Expected: FAIL — `plan_context() got an unexpected keyword argument 'stored_summary'`

- [ ] **Step 3: 实现**

`_compact` 增加一个 `stored: str | None` 参数：非空则 `summary = stored` 且
`source="model"`，否则走现有的 `_summarize` 且 `source="structural"`。
`plan_context` 把新参数透传下去。

**保留现有的 hints 双轮循环**（先带线索词、装不下再不带）：那一轮是为结构摘要
设计的，模型摘要没有线索词那一半，传入 `stored` 时两轮的结果相同，多跑一轮只是
浪费——所以传入 `stored` 时只跑一轮。这一点要写进注释。

- [ ] **Step 4: 跑它，确认绿，并确认没弄坏既有压缩行为**

```bash
uv run --no-sync pytest packages/backend/tests/unit/runs/test_stored_summary.py \
  packages/backend/tests/unit/runs/test_context_budget.py \
  packages/backend/tests/unit/runs/test_compaction_hints.py -q
```
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add packages/backend/tests/unit/runs/test_stored_summary.py
git commit -m "test(runs): 压缩用传进来的那份摘要"
git add packages/backend/src/tiny_hermes/runs/domain/context_budget.py
git commit -m "feat(runs): 摘要是规划器的输入，不是它的产物"
```

---

### Task 3: Worker 生成摘要一次，落库，再规划一次

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/application/worker.py`
- Test: `packages/backend/tests/integration/runs/test_compaction_summary.py`

**Interfaces:**
- Consumes: Task 1 `latest_summary` / `save_summary` / `StoredSummary`；Task 2 的
  `stored_summary=` 参数与 `CompactionRecord.source`。
- Produces: 无新公开接口；`_plan` 的行为改变。

流程（写进 `_plan` 上方的 docstring，因为顺序本身就是设计）：

```
1. latest_summary(session_id) → 有且覆盖范围够用就带上，plan 一次，结束
2. plan 一次（不带摘要）→ 没触发压缩就结束
3. 触发了压缩且 source == "structural" → 调辅助模型生成摘要
   ├─ 成功 → save_summary → 带着它重新 plan 一次 → 用这一份
   └─ 失败 → 保留第 2 步那份结构摘要，本轮照常发出去
```

**「覆盖范围够用」的判据：** 存下来的 `last_sequence` ≥ 本轮压缩要覆盖到的最后一条的
`sequence`。不够用时按 §7.4.2 走**更新**语义：把上一份摘要和新增的那几条一起交给模型，
产出覆盖更宽的一份。

**失败绝不能变成丢消息。** 模型调用失败、超时、返回空，都退回第 2 步已经算好的结构
摘要——那份摘要是现成的，不需要重算。这一级是 §7.4.2 阶梯的第一级。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_compaction_summary.py
"""摘要生成一次就落库，之后每轮读它。

这条测试的判据是**模型被调了几次**。一个每轮重新生成的实现功能上看不出区别，
但会让同一个 Run 重放得到不同上下文，并且每轮多付一次钱。
"""


async def test_the_summary_is_generated_once_and_then_reused(
    worker, store, session_long_enough_to_compact, summarizer_spy
) -> None:
    session_id = session_long_enough_to_compact

    await worker.run_one_round(session_id)
    await worker.run_one_round(session_id)

    assert summarizer_spy.calls == 1
    stored = await store.latest_summary(session_id)
    assert stored is not None
    assert stored.source == "model"


async def test_a_failed_summary_falls_back_and_does_not_drop_messages(
    worker, store, session_long_enough_to_compact, failing_summarizer
) -> None:
    session_id = session_long_enough_to_compact
    before = await store.list_session_messages_count(session_id)

    plan = await worker.run_one_round(session_id)

    assert plan.compacted is not None
    assert plan.compacted.source == "structural"
    assert await store.list_session_messages_count(session_id) == before
    assert await store.latest_summary(session_id) is None


async def test_a_later_compaction_updates_the_previous_summary(
    worker, store, session_long_enough_to_compact, summarizer_spy
) -> None:
    session_id = session_long_enough_to_compact
    await worker.run_one_round(session_id)
    first = await store.latest_summary(session_id)
    await _add_turns(store, session_id, 30)

    await worker.run_one_round(session_id)

    second = await store.latest_summary(session_id)
    assert second is not None and first is not None
    assert second.last_sequence > first.last_sequence
    assert summarizer_spy.last_prompt_contained(first.text)
```

> `summarizer_spy`、`failing_summarizer`、`session_long_enough_to_compact`、
> `_add_turns`、`list_session_messages_count` 是本任务要写的夹具与辅助。
> `worker.run_one_round` 若与现有 Worker 的入口名不符，用真实入口，**不要为测试
> 新增一个只有测试在用的方法**。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_compaction_summary.py -q
```
Expected: FAIL

- [ ] **Step 3: 实现**

新文件 `packages/backend/src/tiny_hermes/runs/domain/summary_prompt.py`，
纯字符串构造、无 I/O：

```python
"""交给辅助模型的那段话。

放在领域层是因为它决定摘要里有什么，而摘要是下一轮上下文的一部分——它和预算
表一样属于「这一轮发送什么」的规则，不属于某个 provider 的调用细节。
"""

_SECTIONS = (
    "目标：用户想达成什么",
    "约束与偏好：风格、口径、明确说过的限制",
    "进展：已完成 / 进行中 / 被阻塞",
    "已作出的决定：连同理由",
    "涉及的对象：文件、资源、外部系统，附一句它们各自的状态",
    "下一步：接下来要做的事",
    "关键事实：具体的值、报错、配置",
)


def summary_prompt(transcript: str, previous: str | None) -> str:
    head = (
        "更新下面这份既有摘要，使它覆盖新增的对话。保留仍然成立的条目，"
        "删掉已经过时的，不要从头重写。"
        if previous
        else "把下面这段对话压成一份结构化摘要。"
    )
    body = "" if previous is None else f"\n\n既有摘要：\n{previous}"
    return (
        f"{head}\n\n"
        f"按这几节输出，没有内容的一节写「无」：\n"
        + "\n".join(f"- {s}" for s in _SECTIONS)
        + "\n\n"
        # 2026-08-26 的事故：模型在图片管道故障期间说过五次「我看不到图」，
        # 管道修好后它把那些当成已确认的事实继续拒绝。把那种话蒸馏进摘要会让它
        # 更短、更权威、也更难推翻。这一句能减轻，**不能消除**——摘要模型读到的
        # 仍然是那些话，它没有任何依据判断哪句是故障产物。
        "只记录发生了什么，不要记录助手声称的能力状态；"
        "助手说过自己做不到某事，不等于它做不到。\n\n"
        f"对话：\n{transcript}{body}"
    )
```

七节对应 §7.4.2 列的七项。`previous` 非空时走**更新**语义，这是 §7.4.2 的要求。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_compaction_summary.py -q
```
Expected: PASS，3 条。

- [ ] **Step 5: 破坏性验证**

把「先查 `latest_summary`」那一步临时去掉（每轮都重新生成），重跑：
Expected: `test_the_summary_is_generated_once_and_then_reused` **FAIL**（`calls == 2`）。
不红说明 spy 没有真的数到模型调用，**先修测试**。恢复后重跑确认绿。

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/runs/test_compaction_summary.py
git commit -m "test(runs): 摘要生成一次，之后每轮读它"
git add packages/backend/src/tiny_hermes/runs/application/worker.py \
        packages/backend/src/tiny_hermes/runs/domain/summary_prompt.py
git commit -m "feat(runs): 旧会话压成一份模型写的摘要，只写一次"
```

---

### Task 4: 辅助端点与「窗口不得更小」的发布校验

**Files:**
- Modify: `packages/backend/src/tiny_hermes/agents/domain/models.py`
- Modify: `packages/backend/src/tiny_hermes/agents/application/`（发布校验所在处，按 `context_budget_unsatisfied` 的抛出点找）
- Modify: `packages/backend/src/tiny_hermes/runs/application/worker.py`
- Test: `packages/backend/tests/unit/agents/test_summary_endpoint.py`

**Interfaces:**
- Consumes: Task 3 的摘要调用点。
- Produces:
  ```python
  # AgentSpec.model_policy 新增可选字段
  summary_endpoint_id: UUID | None = None
  ```
  为 `None` 时用该 AgentVersion 自己的端点。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/agents/test_summary_endpoint.py
"""摘要模型的窗口不得小于主模型的，而且要在发布时就拒绝。

上游 Hermes 把这条记成了「最常见的退化原因」：摘要模型上下文不够时，它的
实现直接丢弃中间轮次且不生成摘要。发布时拦住，就没有运行时那一刻。
"""

import pytest


def test_a_smaller_summary_endpoint_is_refused_at_publish(publisher) -> None:
    with pytest.raises(ContextBudgetUnsatisfied) as raised:
        publisher.publish(_spec(main_window=128_000, summary_window=32_000))

    assert "summary" in str(raised.value).lower()


def test_the_same_size_is_accepted(publisher) -> None:
    publisher.publish(_spec(main_window=128_000, summary_window=128_000))


def test_no_summary_endpoint_means_the_agent_s_own(publisher) -> None:
    version = publisher.publish(_spec(main_window=128_000, summary_window=None))

    assert version.spec.model_policy.summary_endpoint_id is None
```

> `ContextBudgetUnsatisfied` 用发布校验现有的那个异常，不要新增一个；
> `_spec` 与 `publisher` 照 `tests/unit/agents/` 现有夹具写。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/agents/test_summary_endpoint.py -q
```
Expected: FAIL

- [ ] **Step 3: 实现**

字段加在 `model_policy` 上并给默认 `None`；校验放进已有的发布校验路径，
与 `context_budget_unsatisfied` 同一条路，不要新开一条。Worker 解析摘要端点时，
`None` 走该 AgentVersion 自己的端点——**同一端点自动满足窗口约束**，这一点写进注释，
因为它解释了为什么默认值是安全的。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/unit/agents -q
```
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add packages/backend/tests/unit/agents/test_summary_endpoint.py
git commit -m "test(agents): 摘要模型的窗口不得比主模型小"
git add packages/backend/src/tiny_hermes/agents packages/backend/src/tiny_hermes/runs/application/worker.py
git commit -m "feat(agents): 摘要端点可声明，发布时校验窗口"
```

---

### Task 5: 压缩事件说清摘要是谁写的

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/domain/context_budget.py`（`CompactionRecord.payload`）
- Modify: `packages/backend/src/tiny_hermes/runs/application/worker.py`
- Test: `packages/backend/tests/integration/runs/test_compaction_event.py`

**Interfaces:**
- Consumes: Task 2 的 `CompactionRecord.source`；Task 4 的端点解析。
- Produces: `CONTEXT_COMPACTED` 事件的 payload 增加 `source`、`endpoint_id`、`model`。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_compaction_event.py
"""运维看到一段被压缩过的会话，必须分得清模型读到的是什么。

一份语义摘要和一句「这里曾有 38 条消息」对后续每一轮的影响完全不同。事件里
不写，就只能靠猜。
"""


async def test_a_model_summary_says_which_model_wrote_it(
    worker, events, session_long_enough_to_compact, summarizer_spy
) -> None:
    await worker.run_one_round(session_long_enough_to_compact)

    payload = await events.latest_of(session_long_enough_to_compact, "context_compacted")
    assert payload["source"] == "model"
    assert payload["model"]
    assert payload["endpoint_id"]


async def test_a_fallback_says_so_and_names_no_model(
    worker, events, session_long_enough_to_compact, failing_summarizer
) -> None:
    await worker.run_one_round(session_long_enough_to_compact)

    payload = await events.latest_of(session_long_enough_to_compact, "context_compacted")
    assert payload["source"] == "structural"
    assert payload["model"] is None
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_compaction_event.py -q
```
Expected: FAIL — payload 里没有 `source`

- [ ] **Step 3: 实现**

`payload()` 加 `source`；端点与模型名由 Worker 在写事件时补上（领域层不知道端点是谁，
也不该知道）。这一点写进注释。

- [ ] **Step 4: 跑它，确认绿并提交**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_compaction_event.py -q
git add packages/backend/tests/integration/runs/test_compaction_event.py
git commit -m "test(runs): 压缩事件要说清摘要是谁写的"
git add packages/backend/src/tiny_hermes/runs/domain/context_budget.py \
        packages/backend/src/tiny_hermes/runs/application/worker.py
git commit -m "feat(runs): 压缩事件记下摘要的来源与模型"
```

---

### Task 6: 比例触发（最后做）

**Files:**
- Modify: `packages/backend/src/tiny_hermes/agents/domain/models.py`（`ContextBudget`）
- Modify: `packages/backend/src/tiny_hermes/runs/domain/context_budget.py`
- Modify: `packages/backend/src/tiny_hermes/runs/application/worker.py`
- Test: `packages/backend/tests/unit/runs/test_compaction_threshold.py`
- Test: `packages/backend/tests/unit/agents/test_budget_hash.py`

**Interfaces:**
- Consumes: Task 2 的 `stored_summary=`。
- Produces:
  ```python
  # ContextBudget 新增字段
  compaction_threshold: float | None = None    # None = 用平台默认

  DEFAULT_COMPACTION_THRESHOLD = 0.50          # context_budget.py 模块级

  # plan_context 新增关键字参数
  threshold: float = DEFAULT_COMPACTION_THRESHOLD
  ```

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/runs/test_compaction_threshold.py
"""装得下也压——到达比例就动。

原来的判据是「装不下」，于是一段离窗口还很远的长会话永远不会被压缩。2026-08-26
那条 80 条消息的会话就是这样：128k 的窗口，压缩一次都没跑过。
"""


def test_a_history_that_fits_is_still_compacted_past_the_threshold(mid_history) -> None:
    plan = _plan_with(mid_history, threshold=0.50)

    assert plan.fits
    assert plan.compacted is not None


def test_below_the_threshold_nothing_is_compacted(mid_history) -> None:
    plan = _plan_with(mid_history, threshold=0.99)

    assert plan.compacted is None


def test_the_current_request_is_never_compacted_away(mid_history) -> None:
    plan = _plan_with(mid_history, threshold=0.01)

    assert _last_text(plan.messages) == _last_text(mid_history)
```

```python
# packages/backend/tests/unit/agents/test_budget_hash.py
"""不写就不带那个键——已发布 AgentVersion 的内容哈希不得变化。"""


def test_a_spec_that_does_not_set_the_threshold_hashes_as_before() -> None:
    spec = _spec_without_threshold()

    assert "compaction_threshold" not in spec.document()["context_budget"]


def test_setting_it_changes_the_hash() -> None:
    a = _spec_without_threshold()
    b = _spec_with_threshold(0.6)

    assert content_hash(a) != content_hash(b)
```

> `content_hash` 与 `document()` 用 AgentVersion 现有的那套，不要新写一份。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/runs/test_compaction_threshold.py \
  packages/backend/tests/unit/agents/test_budget_hash.py -q
```
Expected: FAIL

- [ ] **Step 3: 实现**

`plan_context` 的压缩判据从「`spent > allowance`」改成
「`spent > allowance * threshold`」，装不下时的原有路径保留为兜底。
**当前请求与 `PROTECTED_RECENT_MESSAGES` 的保护不变**——阈值只改什么时候动手，
不改动手时能碰什么。这一句写进注释，因为把阈值调到 0.01 时它是唯一的护栏。

`ContextBudget.compaction_threshold` 加 `float | None = None`，并确认序列化时
**不写就不带这个键**（这是哈希不变的执行者，不是靠注释保证的）。

**阈值与各段预算同权限**（§7.4.2）：平台配默认值和硬上下限，Agent 开发者只能在
硬上下限内调。加一个 `pydantic` 字段校验，超出 `(0, 1]` 直接拒绝，并在发布校验里
按平台配置的上下限二次拒绝，走 `context_budget_unsatisfied` 那条路——不要新开一条
错误路径。补一条测试：

```python
def test_a_threshold_outside_the_platform_bounds_is_refused(publisher) -> None:
    with pytest.raises(ContextBudgetUnsatisfied):
        publisher.publish(_spec_with_threshold(1.5))
```

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/unit/runs packages/backend/tests/unit/agents -q
```
Expected: PASS

- [ ] **Step 5: 验证已发布版本的哈希真的没变**

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"
uv run --no-sync pytest packages/backend/tests/integration -k "version or publish or hash" -q
```
Expected: PASS。**任何一条关于已发布版本哈希的测试变红，都说明可选字段泄漏进了
文档，必须先修那个再往下。**

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/unit/runs/test_compaction_threshold.py \
        packages/backend/tests/unit/agents/test_budget_hash.py
git commit -m "test(runs): 装得下也压，以及不写就不带那个键"
git add packages/backend/src/tiny_hermes/agents/domain/models.py \
        packages/backend/src/tiny_hermes/runs/domain/context_budget.py \
        packages/backend/src/tiny_hermes/runs/application/worker.py
git commit -m "feat(runs): 压缩到达比例就动，不再等装不下"
```

---

## 收尾

- [ ] **本地全套**：unit、integration、ruff、pyright、web、chat-web。
      `tests/integration/model_catalog/test_endpoint_api.py` 那两条在有仓库根 `.env`
      的机器上会失败，是已知环境问题，不是这条分支的。
- [ ] **部署并真机走一遍**：`deploy/compose/redeploy.sh`，然后在飞书里把一段对话
      聊到过阈值，确认压缩发生、摘要是模型写的、第二轮没有再次调用模型。
      **「测试过了」和「这条路走得通」分开写。**
- [ ] **写验收记录** `docs/superpowers/verification/2026-08-29-context-compaction.md`，
      必须有「这一遍没能证明什么」与「不声称什么」两节。至少要写明：摘要质量没有
      任何度量；多次更新后的漂移没有观察过；提示词里那句「不要记录助手声称的能力
      状态」减轻不了全部风险。
- [ ] **开 PR，取得真正的 compose-e2e 绿色**，并用
      `gh run view <id> --log | grep "^compose-e2e" | grep -E "passed|✘"` 确认它真的跑了测试。
