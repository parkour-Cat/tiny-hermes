# 自动续跑让位于排队消息 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个判为 `continue` 的 Run，在同一 Session 已有排队消息时，以 `completed` 结束并让出队首，使那条消息立即得到处理。

**Architecture:** 「有人在排队」是一个数据库事实，`decide_after_round` 是纯函数拿不到它——由 Worker 在本轮结束后查出来，作为 `RoundOutcome` 的一个字段传进去。判定规则本身留在 `slice_policy.py`，与既有的取消/暂停/预算/审批同一张优先级表。

**Tech Stack:** Python 3.12、SQLAlchemy 2 async、pytest、PostgreSQL。

## Global Constraints

- 产品事实来源：`docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.9 §12.1、§12.3。冲突以它为准。
- **测试先写，跑它，看它红，再实现。提交分开：先 test 再 impl。**
- 注释和 docstring 解释「为什么」，不解释「做了什么」。
- **一条注释不得声称代码没有的保护。**
- **断言按 id 找行，不要按下标。**
- `slice_policy.py` 与 `goal.py` 是纯函数，**不得引入 I/O**。
- 跑测试前两行必须分开写：
  ```
  export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
  export DATABASE_URL="$TEST_DATABASE_URL"
  ```
- **永远只跑一个 pytest。**
- 不改 `pyproject.toml`。
- 部署用 `deploy/compose/redeploy.sh`。

---

### Task 1: 「后面有人在等吗」这个事实

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/ports/store.py`
- Test: `packages/backend/tests/integration/runs/test_waiting_run.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  ```python
  # RunStore 协议与 SqlRunStore
  async def has_waiting_run(self, session_id: UUID, after_sequence: int) -> bool
  ```
  当该 Session 中存在 `session_sequence > after_sequence` 且状态不在
  `TERMINAL_STATES` 的 Run 时返回 `True`。

**不要复用 `unfinished_work`。** 它回答的是另一个问题——「这个 Session 有没有未了结的
工作」，供 `/undo` 与 `/new` 判断能不能动历史，并且在队首自身已终态时返回 `"queued"`。
这里要问的是「**我后面**还有没有人在等」，参照系是当前 Run 的 `session_sequence`。
两个问题的答案在队首终态时会分叉，共用一个函数会让其中一个悄悄答错。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_waiting_run.py
"""后面有没有人在等——判定自动续跑该不该让位的那个事实。

参照系是当前 Run 的 `session_sequence`，不是 `head_run_id`：让位是为了让**后面**
那条消息跑起来，而队首是谁与「我后面有没有人」是两件事。
"""


async def test_a_run_alone_in_its_session_has_nobody_waiting(
    store, session_with_one_running_run
) -> None:
    session_id, run = session_with_one_running_run

    assert await store.has_waiting_run(session_id, run.session_sequence) is False


async def test_a_queued_run_behind_it_is_somebody_waiting(
    store, session_with_a_queued_run_behind
) -> None:
    session_id, head, queued = session_with_a_queued_run_behind

    assert await store.has_waiting_run(session_id, head.session_sequence) is True


async def test_a_terminal_run_behind_it_is_not_waiting(
    store, session_with_a_finished_run_behind
) -> None:
    session_id, head, finished = session_with_a_finished_run_behind

    assert await store.has_waiting_run(session_id, head.session_sequence) is False


async def test_a_run_ahead_of_it_does_not_count(
    store, session_with_a_queued_run_behind
) -> None:
    session_id, head, queued = session_with_a_queued_run_behind

    # 从排队那条自己的角度看，它后面没有人。
    assert await store.has_waiting_run(session_id, queued.session_sequence) is False
```

> 三个夹具是本任务要写的，照 `packages/backend/tests/integration/runs/` 现有的建
> Session 与 Run 的方式写，返回 `(session_id, run)` 或 `(session_id, head, other)`。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_waiting_run.py -q
```
Expected: FAIL — `AttributeError: 'SqlRunStore' object has no attribute 'has_waiting_run'`

- [ ] **Step 3: 实现**

```python
    async def has_waiting_run(self, session_id: UUID, after_sequence: int) -> bool:
        """是否有人排在这个 Run 后面。

        §12.1 的让位规则要的就是这一个事实。参照系是 `session_sequence` 而不是
        `head_run_id`：让位是为了让**后面**那条消息跑起来，而「谁是队首」在这一刻
        必然是提问者自己，问它得不到答案。

        `EXISTS` 而不是取回行：调用方只需要真假，而一个 Session 后面可能排着很多条。
        """
        return bool(
            await self._session.scalar(
                select(
                    exists().where(
                        RunRow.session_id == session_id,
                        RunRow.session_sequence > after_sequence,
                        RunRow.status.not_in(tuple(s.value for s in TERMINAL_STATES)),
                    )
                )
            )
        )
```

`ports/store.py` 的协议加上同一个签名。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_waiting_run.py -q
```
Expected: PASS，4 条。

- [ ] **Step 5: 提交**

```bash
git add packages/backend/tests/integration/runs/test_waiting_run.py
git commit -m "test(runs): 后面有没有人在等"
git add packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py \
        packages/backend/src/tiny_hermes/runs/ports/store.py
git commit -m "feat(runs): 问一句后面还有没有人排着"
```

---

### Task 2: 让位规则，与它必须留下的记录

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/domain/slice_policy.py`
- Modify: `packages/backend/src/tiny_hermes/runs/domain/models.py`（`RunSnapshot._goal_document`）
- Test: `packages/backend/tests/unit/runs/test_goal_preemption.py`

**Interfaces:**
- Consumes: 无（纯函数，事实由调用方传入）。
- Produces:
  ```python
  # RoundOutcome 新增字段（最后一个，有默认值）
  user_waiting: bool = False

  # RunSnapshot 新增字段（默认 False）
  goal_preempted: bool = False
  # _goal_document() 的返回值新增一键
  {"round": ..., "outcome": ..., "unmet": [...], "preempted": bool}
  ```

**优先级放在哪：** 在 `WAIT` 之后、`compat_window_expired` 之前。理由要写进注释——
取消、暂停、预算、审批、委派仍然排在它前面（那些是「必须停」，让位是「该停」），
而 `done` / `failed` / `undecidable` / `wait` 都是已经决定了 Run 去向的裁决，让位只
接管本来会继续的那一种。

**`completed` 必须配 `preempted`，这是 §12.1 的硬性要求。** 一个没达成目标的 Run 在
列表里显示为 `completed` 已经够容易误读；若连原因都不留，运维只能猜。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/runs/test_goal_preemption.py
"""判为 continue 的一轮，后面有人在等时该让位。

让位的理由是产品的：用户在任务跑到一半时说的话，几乎总是「我要改主意」或
「这不对」，让他等到任务跑完才被听见，是把机器的进度排在人的意图前面。
"""

from tiny_hermes.runs.domain.models import PauseReason, RunSignal
from tiny_hermes.runs.domain.goal import GoalOutcome
from tiny_hermes.runs.domain.slice_policy import RoundOutcome, decide_after_round


def test_continue_with_somebody_waiting_completes_the_run() -> None:
    decision = decide_after_round(_continuing(user_waiting=True))

    assert decision.signal is RunSignal.COMPLETED


def test_continue_with_nobody_waiting_keeps_going() -> None:
    decision = decide_after_round(_continuing(user_waiting=False))

    assert decision.signal is not RunSignal.COMPLETED


def test_a_cancellation_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(_continuing(user_waiting=True, cancel_requested=True))

    assert decision.signal is RunSignal.SAFE_CANCEL_STARTED


def test_a_pause_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(_continuing(user_waiting=True, pause_requested=True))

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED


def test_a_done_verdict_is_not_turned_into_a_preemption() -> None:
    # `done` 已经决定了 Run 的去向，让位只接管本来会继续的那一种。
    decision = decide_after_round(
        _continuing(user_waiting=True, outcome=GoalOutcome.DONE)
    )

    assert decision.signal is RunSignal.COMPLETED
```

```python
# 同一文件，记录那一半
from tiny_hermes.runs.domain.models import RunSnapshot


def test_the_goal_document_says_it_was_preempted() -> None:
    document = _snapshot(goal_preempted=True).document()

    assert document["goal"]["preempted"] is True


def test_an_ordinary_run_says_it_was_not() -> None:
    document = _snapshot(goal_preempted=False).document()

    assert document["goal"]["preempted"] is False
```

构造辅助，放在同一文件里：

```python
def _continuing(
    *,
    user_waiting: bool,
    cancel_requested: bool = False,
    pause_requested: bool = False,
    outcome: GoalOutcome = GoalOutcome.CONTINUE,
) -> RoundOutcome:
    """一轮判为 continue 的结果，除了要试的那一项之外什么都不拦着它继续。

    默认值全部设成「不会让 Run 停下来」的那一侧，这样任何一条测试变红时，
    红的原因只能是它自己改的那一项。
    """
    return RoundOutcome(
        verdict=GoalVerdict(outcome=outcome, unmet=(), instruction="继续"),
        approval=None,
        delegated=None,
        cancel_requested=cancel_requested,
        pause_requested=pause_requested,
        budget_allows=True,
        slice_expired=False,
        user_waiting=user_waiting,
    )
```

> `GoalVerdict` 的确切字段以 `runs/domain/goal.py` 为准；若与上面不符，用真实的，
> 并把差异写进报告。`_snapshot(...)` 照 `tests/unit/runs/` 里现有构造 `RunSnapshot`
> 的方式写，**不要新造一套夹具体系**。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/runs/test_goal_preemption.py -q
```
Expected: FAIL — `RoundOutcome() got an unexpected keyword argument 'user_waiting'`

- [ ] **Step 3: 实现**

`RoundOutcome` 末尾加 `user_waiting: bool = False`，注释写明这是**调用方查出来的库里的
事实**，这一层拿不到也不该拿。

`decide_after_round` 在 `WAIT` 分支之后、`compat_window_expired` 之前插入：

```python
    if outcome.user_waiting:
        # §12.1：判为 `continue` 的一轮，在同一 Session 已有排队消息时让出队首。
        # 位置是产品规则不是实现方便：取消、暂停、预算、审批、委派仍排在前面
        # ——那些是「必须停」，这一条是「该停」；而 done/failed/undecidable/wait
        # 都已经决定了 Run 的去向，这里只接管本来会继续的那一种。
        #
        # `completed` 而不是 paused：暂停的 Run 仍然占着队首，排队那条照样跑不了，
        # 让位就没有发生。§564 只有终态才让出队首。
        return SliceDecision(RunSignal.COMPLETED)
```

`RunSnapshot` 加 `goal_preempted: bool = False`，`_goal_document()` 增加 `"preempted"` 键。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/unit/runs -q
```
Expected: PASS

- [ ] **Step 5: 破坏性验证**

把新加的 `if outcome.user_waiting:` 整段临时删掉，重跑：
Expected: `test_continue_with_somebody_waiting_completes_the_run` **FAIL**。
不红说明测试没有真的走到那条分支，**先修测试**。恢复后确认绿。

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/unit/runs/test_goal_preemption.py
git commit -m "test(runs): 有人在排队时，continue 该让位"
git add packages/backend/src/tiny_hermes/runs/domain/slice_policy.py \
        packages/backend/src/tiny_hermes/runs/domain/models.py
git commit -m "feat(runs): 自动续跑让位于排队中的用户消息"
```

---

### Task 3: 接线，与一条真的走完的路

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/application/worker.py`（`RoundOutcome` 构造点，约 512 行；写 `goal_outcome` 的检查点，约 2692 行）
- Test: `packages/backend/tests/integration/runs/test_preemption_flow.py`

**Interfaces:**
- Consumes: Task 1 的 `has_waiting_run`；Task 2 的 `RoundOutcome.user_waiting`、
  `RunSnapshot.goal_preempted`。
- Produces: 无新接口。

**这一档专抓「够得着」。** 这个项目最常见的 bug 是值写进去了、没人读得到——已经抓到
至少九次，其中四次就在最近这条压缩分支上，**每一次后端测试都是全绿的**，因为断言都
停在真正要紧的那一层前面一层。所以本任务的判据不是「`decide_after_round` 返回了
COMPLETED」，而是**排队的那条消息真的跑起来了**。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_preemption_flow.py
"""让位这条路要真的走通：Run 结束、队首让出、排队那条真的跑起来。

断言停在 `decide_after_round` 的返回值上是不够的——让位的全部意义是后面那条
消息得到处理，而那要跨过 Run 终态、队首推进和 Worker 领取三道关。
"""


async def test_a_waiting_message_actually_runs_after_the_preemption(
    worker, store, session_with_a_continuing_run_and_a_queued_message
) -> None:
    session_id, running, queued = session_with_a_continuing_run_and_a_queued_message

    await worker.run_one_slice()

    assert (await store.read_run(running.id)).status == "completed"
    assert (await store.read_run(queued.id)).status != "queued"


async def test_the_preempted_run_says_why_it_ended(
    worker, store, session_with_a_continuing_run_and_a_queued_message
) -> None:
    session_id, running, _ = session_with_a_continuing_run_and_a_queued_message

    await worker.run_one_slice()

    snapshot = await store.read_run(running.id)
    assert snapshot.goal_preempted is True
    assert snapshot.document()["goal"]["preempted"] is True


async def test_a_run_with_nobody_waiting_keeps_going(
    worker, store, session_with_a_continuing_run_alone
) -> None:
    session_id, running = session_with_a_continuing_run_alone

    await worker.run_one_slice()

    assert (await store.read_run(running.id)).status != "completed"
```

> 两个夹具与 `worker.run_one_slice` 要用 Worker 的**真实入口**；若名字不符，用真实的，
> **不要为测试新增一个只有测试在调的方法**。
>
> `store.read_run(...)` 同样是占位写法——用仓库里真实的读取路径取回 `RunSnapshot`
> （`tests/integration/runs/` 里已有多处这么做），它要能给出 `status`、
> `goal_preempted` 和 `document()`。若真实入口拿不到其中某一项，**先说出来再动手**，
> 不要为了让断言通过而绕道去查库——绕过去就等于不再验证「够得着」这件事。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_preemption_flow.py -q
```
Expected: FAIL — 让位的 Run 仍然在续跑，排队那条仍是 `queued`

- [ ] **Step 3: 实现**

在 `worker.py` 构造 `RoundOutcome` 之前查出事实并传入：

```python
                waiting = await self._has_waiting_run(claimed)
                ...
                decision = decide_after_round(
                    RoundOutcome(
                        ...
                        user_waiting=waiting,
                    )
                )
```

```python
    async def _has_waiting_run(self, claimed: ClaimedRun) -> bool:
        """这一轮结束时，后面还有没有人排着。

        每轮一次查询，和 `_cost_precheck` 读预算是同一个量级的代价；缓存它会让
        这一轮做的判断依据上一轮的事实，而「有人在等」恰恰是可能在一轮之内变真的。
        """
        async with self._sessions.begin() as session:
            return await SqlRunStore(session).has_waiting_run(
                claimed.run.session_id, claimed.run.session_sequence
            )
```

写检查点的地方（约 2692 行）把让位记下来，与 `goal_outcome` 同一处：

```python
        checkpoint["goal_preempted"] = decision.signal is RunSignal.COMPLETED and (
            judged.verdict.outcome is GoalOutcome.CONTINUE
        )
```

**注释要写明为什么判据是这两者的合取**：单看信号分不出「做完了」和「被打断了」，
单看裁决分不出「continue 但被打断」和「continue 且继续了」。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_preemption_flow.py \
  packages/backend/tests/integration/runs/test_waiting_run.py -q
```
Expected: PASS

- [ ] **Step 5: 确认没弄坏既有的轮转**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs -q
```
Expected: PASS。让位规则动的是 Run 什么时候结束，**既有的多轮任务测试若开始变红，
说明 `user_waiting` 在不该为真的时候为真**，先查那个再往下。

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/runs/test_preemption_flow.py
git commit -m "test(runs): 让位之后，排队那条真的跑起来"
git add packages/backend/src/tiny_hermes/runs/application/worker.py
git commit -m "feat(runs): 本轮结束时看一眼后面有没有人在等"
```

---

## 收尾

- [ ] **本地全套**：`alembic check`、unit、integration、ruff、pyright、web、chat-web。
      `tests/integration/model_catalog/test_endpoint_api.py` 那两条在有仓库根 `.env` 的
      机器上会失败，是已知环境问题。
- [ ] **部署并真机走一遍**：`deploy/compose/redeploy.sh`，然后在飞书里让 Agent 开始一个
      会续跑的任务，**在它跑的过程中发一句话**，确认：那句话立刻得到处理、上一个 Run
      是 `completed`、且它的 `goal.preempted` 为 `true`。
      **「测试过了」和「这条路走得通」分开写。**
- [ ] **写验收记录** `docs/superpowers/verification/2026-08-30-goal-preemption.md`，
      必须有「这一遍没能证明什么」与「不声称什么」两节。至少写明：没有验证过用户
      **连续**插话时会不会把一个任务打断到永远做不完；没有度量过被打断的任务重新
      「继续」时模型能不能接上。
- [ ] **开 PR，取得真正的 compose-e2e 绿色**，用
      `gh run view <id> --log | grep "^compose-e2e" | grep -E "passed|✘"` 确认。
