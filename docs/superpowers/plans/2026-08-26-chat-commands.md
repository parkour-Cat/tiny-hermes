# 聊天内命令 /undo 与 /new 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让飞书用户能用 `/undo` 撤回上一轮、用 `/new` 开始一段干净的对话，而不必有人去删数据库。

**Architecture:** 渠道领域层做纯解析；撤回逻辑放在 `RunCoordination`（Web Chat 以后接同一个操作）；被撤的消息软隐藏（`withdrawn_at`），不删除；回执沿用 `BlockedNotice` 的「存结构化文档、边缘渲染」写法，走已有的「无 Run 但欠人一句话」扫描路径。

**Tech Stack:** Python 3.12、SQLAlchemy 2 async、Alembic、pytest、PostgreSQL。

## Global Constraints

- 设计事实来源：`docs/superpowers/specs/2026-08-26-chat-commands-design.md`。与它冲突以它为准。
- **测试先写，跑它，看它红，再实现。提交分开：先 test 再 impl。**
- 注释和 docstring 解释「为什么」，不解释「做了什么」。
- **一条注释不得声称代码没有的保护。**
- 断言按 id 找行，不要按下标。
- 迁移 head 当前是 `20260825_0046`；本计划新增 `20260826_0047`、`20260826_0048`，按此顺序串。
- 跑测试前两行必须分开写：
  ```
  export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
  export DATABASE_URL="$TEST_DATABASE_URL"
  ```
- **永远只跑一个 pytest。**
- 不改 `pyproject.toml`。

---

### Task 1: 领域层解析命令

**Files:**
- Create: `packages/backend/src/tiny_hermes/channels/domain/commands.py`
- Test: `packages/backend/tests/unit/channels/test_commands.py`

**Interfaces:**
- Consumes: 无。
- Produces: `CommandName`（`StrEnum`，成员 `UNDO="undo"`、`NEW="new"`）、
  `ChatCommand`（frozen dataclass，字段 `name: CommandName`、`turns: int = 1`）、
  `parse(text: str, *, has_images: bool = False) -> ChatCommand | None`。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/channels/test_commands.py
"""命令解析：认得出该认的，认不出不该认的。

第二类比第一类重要。渠道里一条 `/usr/local/bin` 或一句玩笑被当成命令吞掉，
用户看到的是消息石沉大海——比命令不好用糟得多。
"""

import pytest

from tiny_hermes.channels.domain.commands import ChatCommand, CommandName, parse


@pytest.mark.parametrize(
    "text",
    ["/undo", "/UNDO", "  /undo  ", "/Undo"],
)
def test_undo_is_recognised_whatever_the_case_or_padding(text: str) -> None:
    assert parse(text) == ChatCommand(name=CommandName.UNDO, turns=1)


def test_undo_takes_a_turn_count() -> None:
    assert parse("/undo 3") == ChatCommand(name=CommandName.UNDO, turns=3)


@pytest.mark.parametrize("text", ["/new", "/reset", "/NEW"])
def test_new_and_its_alias(text: str) -> None:
    assert parse(text) == ChatCommand(name=CommandName.NEW, turns=1)


@pytest.mark.parametrize(
    "text",
    [
        "/undoing",
        "/undo 顺便帮我看看这个",
        "/usr/local/bin",
        "/newsletter",
        "undo",
        "",
        "/undo -1",
        "/undo 0",
        "/undo abc",
        "/new 3",
    ],
)
def test_what_must_not_be_swallowed(text: str) -> None:
    assert parse(text) is None


def test_a_message_carrying_an_image_is_never_a_command() -> None:
    assert parse("/undo", has_images=True) is None
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_commands.py -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'tiny_hermes.channels.domain.commands'`

- [ ] **Step 3: 实现**

```python
# packages/backend/src/tiny_hermes/channels/domain/commands.py
"""聊天里输入的命令，认出来或者不认。

只认**整条消息精确匹配**的几个名字。其它以 `/` 开头的一律不认，原样交给模型
——渠道里一条路径、一个日期、一句玩笑以 `/` 开头太常见，把它们吞掉的代价
（消息看起来石沉大海）远大于漏认一条命令。

纯函数，无 I/O：命令是什么，与谁发的、会话在哪、能不能执行无关。
"""

from dataclasses import dataclass
from enum import StrEnum


class CommandName(StrEnum):
    UNDO = "undo"
    NEW = "new"


@dataclass(frozen=True)
class ChatCommand:
    name: CommandName
    #: 只对 `UNDO` 有意义；`NEW` 永远是 1，因为它不接受参数。
    turns: int = 1


_NAMES: dict[str, CommandName] = {
    "/undo": CommandName.UNDO,
    "/new": CommandName.NEW,
    "/reset": CommandName.NEW,
}


def parse(text: str, *, has_images: bool = False) -> ChatCommand | None:
    """整条消息是不是一条命令。

    `has_images` 让一条配图的 `/undo` 不是命令：带附件的消息几乎总是在说别的事，
    而误撤是不可见的——用户不会知道自己刚丢了一轮对话。
    """
    if has_images:
        return None
    words = text.strip().split()
    if not words:
        return None
    name = _NAMES.get(words[0].lower())
    if name is None:
        return None
    if name is CommandName.NEW:
        return ChatCommand(name=name) if len(words) == 1 else None
    if len(words) == 1:
        return ChatCommand(name=name)
    if len(words) > 2:
        return None
    if not words[1].isdigit() or int(words[1]) < 1:
        return None
    return ChatCommand(name=name, turns=int(words[1]))
```

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_commands.py -q
```
Expected: PASS，13 条以上。

- [ ] **Step 5: 分两次提交**

```bash
git add packages/backend/tests/unit/channels/test_commands.py
git commit -m "test(channels): 一条命令，和一堆不该被当成命令的东西"
git add packages/backend/src/tiny_hermes/channels/domain/commands.py
git commit -m "feat(channels): 认出聊天里输入的 /undo 与 /new"
```

---

### Task 2: `withdrawn_at` 列，与模型上下文的过滤

**Files:**
- Create: `migrations/versions/20260826_0047_message_withdrawal.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/tables.py`（`SessionMessageRow`，`redacted` 那一行之后）
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py:824`（`execution_context`）
- Test: `packages/backend/tests/integration/runs/test_message_withdrawal.py`

**Interfaces:**
- Consumes: 无。
- Produces: `SessionMessageRow.withdrawn_at: Mapped[datetime | None]`；
  `execution_context` 返回的 `history` 不再包含 `withdrawn_at` 非空的行。

- [ ] **Step 1: 写失败的测试**

测试用现有 integration 夹具建 Session 与消息。**断言按 id 找行，不要按下标。**

```python
# packages/backend/tests/integration/runs/test_message_withdrawal.py
"""撤回的消息不进模型上下文——查的是上下文，不是数据库。

这个项目最常见的 bug 是「写进去了不等于有人够得着」。它的镜像同样成立：
把一行标记成已撤回，不等于每一条读它的路都看不到。所以这里断言的是
`execution_context` 交出来的 history，而不是那一列的值。
"""

from datetime import UTC, datetime


async def test_a_withdrawn_message_is_not_in_the_next_request(
    store, seeded_session_with_two_messages
) -> None:
    session_id, first_id, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    context = await store.execution_context(_a_new_run_in(session_id))

    assert [m.id for m in context.history] == [first_id]


async def test_the_withdrawn_row_is_still_in_the_database(
    store, seeded_session_with_two_messages
) -> None:
    _, _, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    assert await store.withdrawn_at_of(second_id) is not None
```

> 实施者注意：`seeded_session_with_two_messages`、`_a_new_run_in`、`store.withdrawn_at_of`
> 是本任务要写的夹具/辅助，放在同一个测试文件里；`mark_withdrawn` 是 Step 3 要加的
> 真实 store 方法。先照上面写，跑红。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_message_withdrawal.py -q
```
Expected: FAIL — `AttributeError: 'SqlRunStore' object has no attribute 'mark_withdrawn'`

- [ ] **Step 3: 加列、加迁移、加过滤**

迁移：

```python
# migrations/versions/20260826_0047_message_withdrawal.py
"""一条消息可以被它的作者收回，而不被删掉。

软隐藏而非删除，因为 `runs/domain/context_budget.py` 写下的不变量是
「No branch makes a message unreachable」。撤回把消息挡在模型上下文之外，
不把它挡在转写记录和审计之外。

与 `redacted` 是两件事，所以是两列：`redacted` 是 §344 的擦除（等于不存在），
撤回是「用户收回了」——转写记录仍然要显示它，标为已撤回。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0047"
down_revision: str | None = "20260825_0046"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "session_messages",
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_messages", "withdrawn_at")
```

`tables.py`，紧接 `redacted` 之后：

```python
    #: 用户收回了它。与 `redacted` 分开的原因写在 20260826_0047：擦除等于不存在，
    #: 收回仍然要出现在转写记录里。
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

`sql_store.py:824` 的 `scoped`，加一个条件：

```python
        scoped = select(SessionMessageRow).where(
            SessionMessageRow.session_id == run.session_id,
            SessionMessageRow.redacted.is_(False),
            SessionMessageRow.withdrawn_at.is_(None),
        )
```

再加两个 store 方法（`mark_withdrawn` 供 Task 4 使用，`withdrawn_at_of` 只供测试断言）：

```python
    async def mark_withdrawn(self, message_ids: Sequence[UUID], *, at: datetime) -> int:
        """置时间戳，且只置一次。

        `withdrawn_at.is_(None)` 不是防御性的多余条件：撤回是幂等的，重放同一条
        命令不得把第一次撤回的时刻改写成第二次的。
        """
        if not message_ids:
            return 0
        result = await self._session.execute(
            update(SessionMessageRow)
            .where(
                SessionMessageRow.id.in_(message_ids),
                SessionMessageRow.withdrawn_at.is_(None),
            )
            .values(withdrawn_at=at)
        )
        await self._session.flush()
        return result.rowcount

    async def withdrawn_at_of(self, message_id: UUID) -> datetime | None:
        row = await self._session.get(SessionMessageRow, message_id)
        return None if row is None else row.withdrawn_at
```

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_message_withdrawal.py -q
```
Expected: PASS

- [ ] **Step 5: 破坏性验证**

把 `sql_store.py` 里刚加的 `SessionMessageRow.withdrawn_at.is_(None)` 那一行临时删掉，重跑：

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_message_withdrawal.py -q
```
Expected: `test_a_withdrawn_message_is_not_in_the_next_request` **FAIL**。确认后把这行加回来。
若它没有变红，说明测试没有真的走过 `execution_context`，**必须先修测试再往下**。

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/runs/test_message_withdrawal.py
git commit -m "test(runs): 撤回的消息不进下一轮请求"
git add migrations/versions/20260826_0047_message_withdrawal.py \
        packages/backend/src/tiny_hermes/runs/infrastructure/tables.py \
        packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py
git commit -m "feat(runs): 一条消息可以被收回而不被删掉"
```

---

### Task 3: 其余四个读点的判定落地

设计 §5.1 列了五个读 `session_messages` 的地方。Task 2 处理了第一个。这一条把剩下四个落实。
**这是本计划最容易漏的一环**：撤了不等于每条路都看不到。

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py:1258`（`_child_result`）
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py:2581`（`list_session_messages`）
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py:2654`（`_copy_checkpoint_messages`）
- Modify: `packages/backend/src/tiny_hermes/memory/infrastructure/sql_search.py:100`（`_base`）
- Test: `packages/backend/tests/integration/runs/test_withdrawal_reach.py`

**Interfaces:**
- Consumes: Task 2 的 `withdrawn_at` 列与 `mark_withdrawn`。
- Produces: 无新接口；四处查询行为改变。

判定表（照抄设计 §5.1，实现必须与它一致）：

| 位置 | 判定 |
|---|---|
| `_child_result` | **过滤**——撤了就不该被当作子 Run 的结果引用 |
| `list_session_messages` | **不过滤**，但必须把 `withdrawn_at` 带进返回值 |
| `_copy_checkpoint_messages` | **过滤**——撤回的不该被复制进新检查点 |
| `sql_search._base` | **过滤**（设计 §3.1：搜得回来就等于没撤） |

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_withdrawal_reach.py
"""撤回之后，还有哪些路看得见它。

设计 §5.1 的判定表，逐条钉住。会话搜索那条尤其要紧：压缩摘要会主动告诉模型
「searchable with session.search」并附上线索词，被撤的内容若还搜得回来，
撤回就是漏的——而这正是这个功能要修的东西。
"""

from datetime import UTC, datetime


async def test_session_search_does_not_find_a_withdrawn_message(
    store, search, seeded_session_with_two_messages
) -> None:
    session_id, _, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    hits = await search.search(workspace_id=workspace.id, query="三视图")

    assert second_id not in {hit.message_id for hit in hits}


async def test_the_transcript_still_shows_it_and_says_it_was_withdrawn(
    store, seeded_session_with_two_messages
) -> None:
    session_id, _, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    listed = await store.list_session_messages(
        workspace_id=workspace.id, session_id=session_id
    )

    shown = next(m for m in listed if m.id == second_id)
    assert shown.withdrawn_at is not None
```

`workspace` 与 `search` 夹具见 `packages/backend/tests/integration/memory/` 下现有用法。

余下两个读点：

```python
async def test_a_withdrawn_assistant_message_is_not_a_child_run_result(
    store, parent_and_child_run
) -> None:
    """子 Run 的结果取的是它最近一条 assistant 消息。撤掉的那条不该顶上来。"""
    parent, child, earlier_answer_id, answer_id = parent_and_child_run
    await store.mark_withdrawn([answer_id], at=datetime.now(UTC))

    result = await store.child_result_for(parent.id)

    # 断言它取到了**前一条**未撤回的 assistant 消息，而不是「没取到」——
    # 一个把结果整个丢掉的实现同样能让「不等于 answer_id」通过。
    assert result is not None
    assert result["message_id"] == earlier_answer_id


async def test_a_withdrawn_message_is_not_copied_into_a_checkpoint(
    store, session_with_a_checkpoint
) -> None:
    """检查点是拿来继续的。带着一段用户已经收回的历史继续，等于没收回。"""
    session_id, source_run, withdrawn_id = session_with_a_checkpoint
    await store.mark_withdrawn([withdrawn_id], at=datetime.now(UTC))

    copied = await store.copy_checkpoint_messages(session_id, source_run)

    assert withdrawn_id not in {row.id for row in copied}
```

> `child_result_for` / `copy_checkpoint_messages` 是 `_child_result` /
> `_copy_checkpoint_messages` 的测试入口。若现有代码只有私有方法，本任务里
> 把它们提成可测的窄公开方法，**不要在测试里调私有名**。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_withdrawal_reach.py -q
```
Expected: FAIL — 搜索仍然命中；转写返回值没有 `withdrawn_at` 字段。

- [ ] **Step 3: 改四处**

三处加同一个条件：

```python
            SessionMessageRow.withdrawn_at.is_(None),
```

分别加进 `_child_result`（1258 起那个 `select`）、`_copy_checkpoint_messages`（2654 起那个 `select`）、
`sql_search._base`（100 行那个 `where`）。每一处上方写一行说明**为什么这一处要过滤**，
不要复制同一句话——`_child_result` 是「撤了就不该被当作结果」，
`_copy_checkpoint_messages` 是「撤回的不该进新检查点」，`_base` 是「搜得回来就等于没撤」。

`list_session_messages`（2581）**不加条件**，改的是返回值：把 `withdrawn_at` 带进它构造的
DTO，并在那里写明为什么这一处不过滤。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs -q
```
Expected: PASS

- [ ] **Step 5: 确认没有第六处读点**

```bash
grep -rn "SessionMessageRow" packages/backend/src/tiny_hermes | grep -v pycache | grep -vE "SessionMessageRow\(|import"
```
逐行对照设计 §5.1 的表。**若出现表里没有的读点，必须把它补进设计文档并说明判定理由，再继续。**

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/runs/test_withdrawal_reach.py
git commit -m "test(runs): 撤回之后，哪些路还看得见它"
git add packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py \
        packages/backend/src/tiny_hermes/memory/infrastructure/sql_search.py
git commit -m "feat(runs): 撤回的消息对上下文、子结果、检查点和搜索都不可见"
```

---

### Task 4: `RunCoordination.withdraw_from_session`

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/domain/models.py`（加 `WithdrawScope`、`Withdrawal`）
- Modify: `packages/backend/src/tiny_hermes/runs/application/service.py`（加 `SessionBusy`、`withdraw_from_session`）
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`（加 `busy_reason`、`withdraw_session_messages`）
- Modify: `packages/backend/src/tiny_hermes/runs/ports/store.py`（协议加两个方法）
- Test: `packages/backend/tests/integration/runs/test_withdraw_service.py`

**Interfaces:**
- Consumes: Task 2 的 `mark_withdrawn`。
- Produces:
  ```python
  class WithdrawScope(StrEnum):
      LAST_EXCHANGE = "last_exchange"
      ALL = "all"

  @dataclass(frozen=True)
  class Withdrawal:
      messages: int          # 实际标记的行数
      turns: int             # 实际撤掉的 user 轮数
      echoed_text: str       # 被撤的那条 user 消息原文，供用户改后重发

  class SessionBusy(RunCoordinationError):
      def __init__(self, reason: str) -> None: ...   # reason ∈ {"running", "queued"}

  # RunCoordination
  async def withdraw_from_session(
      self, session_id: UUID, scope: WithdrawScope, *, turns: int = 1
  ) -> Withdrawal | None      # None = 没有可撤的
  ```

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/runs/test_withdraw_service.py
"""撤回这个操作本身：撤对了几条，什么时候拒绝。

拒绝那条要断言的是**一行都没动**。一个「拒绝了但顺手改了几行」的实现，
测试只看返回值是抓不到的。
"""

import pytest

from tiny_hermes.runs.application.service import SessionBusy
from tiny_hermes.runs.domain.models import WithdrawScope


async def test_undo_takes_back_the_last_user_turn_and_what_followed(
    coordination, finished_session_of_four_messages
) -> None:
    session_id, ids = finished_session_of_four_messages   # user, assistant, user, assistant

    done = await coordination.withdraw_from_session(
        session_id, WithdrawScope.LAST_EXCHANGE, turns=1
    )

    assert done is not None
    assert done.messages == 2
    assert done.turns == 1


async def test_undo_clamps_when_asked_for_more_turns_than_exist(
    coordination, finished_session_of_four_messages
) -> None:
    session_id, _ = finished_session_of_four_messages

    done = await coordination.withdraw_from_session(
        session_id, WithdrawScope.LAST_EXCHANGE, turns=99
    )

    assert done is not None
    assert done.turns == 2
    assert done.messages == 4


async def test_new_takes_back_everything(
    coordination, finished_session_of_four_messages
) -> None:
    session_id, _ = finished_session_of_four_messages

    done = await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert done is not None
    assert done.messages == 4


async def test_a_session_with_work_in_flight_refuses_and_changes_nothing(
    coordination, store, session_with_a_running_run
) -> None:
    session_id, ids = session_with_a_running_run

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert raised.value.reason == "running"
    for message_id in ids:
        assert await store.withdrawn_at_of(message_id) is None


async def test_withdrawing_twice_does_not_move_the_timestamp(
    coordination, store, finished_session_of_four_messages
) -> None:
    session_id, ids = finished_session_of_four_messages
    await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)
    first = await store.withdrawn_at_of(ids[0])

    again = await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert again is None
    assert await store.withdrawn_at_of(ids[0]) == first
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_withdraw_service.py -q
```
Expected: FAIL — `ImportError: cannot import name 'SessionBusy'`

- [ ] **Step 3: 实现**

`runs/domain/models.py` 追加：

```python
class WithdrawScope(StrEnum):
    """撤到哪里为止。"""

    LAST_EXCHANGE = "last_exchange"
    ALL = "all"


@dataclass(frozen=True)
class Withdrawal:
    """一次撤回做成了什么。

    `turns` 是**实际**撤掉的轮数而不是请求的轮数：请求 99 轮而只有 2 轮时，
    回执必须说 2，否则用户以为自己丢了 99 轮。
    """

    messages: int
    turns: int
    echoed_text: str
```

`runs/infrastructure/sql_store.py` 追加两个方法：

```python
    async def busy_reason(self, session_id: UUID) -> str | None:
        """这个 Session 现在有没有未了结的工作，有的话是哪一种。

        判据是「有没有非终态的 Run」，不是 `head_run_id` 是否为空——排在队首
        之后、还没被提上来的 Run 同样是未了结的工作，而 `head_run_id` 对它们
        一无所知。

        先锁 Session 行：撤回与提交新 Run 必须串行，否则一个刚判完「不忙」的
        撤回会和一个正在写入的 Run 交错，撤掉一条对方即将读的历史。
        """
        await self._session.execute(
            select(SessionRow.id).where(SessionRow.id == session_id).with_for_update()
        )
        rows = (
            await self._session.execute(
                select(RunRow.id, RunRow.status).where(
                    RunRow.session_id == session_id,
                    RunRow.status.not_in(tuple(s.value for s in TERMINAL_STATES)),
                )
            )
        ).all()
        if not rows:
            return None
        owning = await self._session.get(SessionRow, session_id)
        head = None if owning is None else owning.head_run_id
        return "running" if any(row.id == head for row in rows) else "queued"

    async def withdrawable(
        self, session_id: UUID, scope: WithdrawScope, turns: int
    ) -> tuple[list[UUID], int, str]:
        """要撤的行、实际轮数、被撤那条 user 消息的原文。

        `LAST_EXCHANGE` 的锚点必须是 user 消息（上游同样如此）：从一条 assistant
        消息往回撤，撤出来的是半轮，重发时对话会错位。
        """
        found = (
            await self._session.scalars(
                select(SessionMessageRow)
                .where(
                    SessionMessageRow.session_id == session_id,
                    SessionMessageRow.redacted.is_(False),
                    SessionMessageRow.withdrawn_at.is_(None),
                )
                .order_by(SessionMessageRow.sequence)
            )
        ).all()
        if not found:
            return [], 0, ""
        if scope is WithdrawScope.ALL:
            anchors = [row for row in found if row.role == "user"]
            text = _text_of(anchors[0]) if anchors else ""
            return [row.id for row in found], len(anchors), text
        users = [row for row in found if row.role == "user"]
        if not users:
            return [], 0, ""
        index = max(len(users) - turns, 0)
        anchor = users[index]
        taken = [row for row in found if row.sequence >= anchor.sequence]
        return [row.id for row in taken], len(users) - index, _text_of(anchor)
```

> `_text_of(row)` 是本任务要加的模块级小函数：从 `row.content` 的 `parts` 里
> 取出 `type == "text"` 的片段拼起来。`sql_store.py` 里已有 `_to_message`，
> 照它的读法写。

`runs/application/service.py` 追加：

```python
class SessionBusy(RunCoordinationError):
    """有未了结的工作时，历史不能在半空中被改写。

    上游 Hermes 选择在这里强行拆掉在飞的工作，代价是三个公开 issue（事件循环
    被拆卸卡死、僵尸槽位静默丢消息、悬空子 agent 烧 token）。tiny-hermes 的
    沙箱所有权和 Session FIFO 语义更强，拆得更贵——而且工具副作用已经发生，
    把那一轮从上下文抹掉不会把副作用抹掉。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
```

```python
    async def withdraw_from_session(
        self, session_id: UUID, scope: WithdrawScope, *, turns: int = 1
    ) -> Withdrawal | None:
        """把一段历史挡在后续上下文之外。返回 `None` 表示没有可撤的。"""
        reason = await self.store.busy_reason(session_id)
        if reason is not None:
            raise SessionBusy(reason)
        ids, taken, text = await self.store.withdrawable(session_id, scope, turns)
        if not ids:
            return None
        changed = await self.store.mark_withdrawn(ids, at=datetime.now(UTC))
        if changed == 0:
            return None
        return Withdrawal(messages=changed, turns=taken, echoed_text=text)
```

`runs/ports/store.py` 的协议加上 `busy_reason`、`withdrawable`、`mark_withdrawn` 三个方法签名。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/runs/test_withdraw_service.py -q
```
Expected: PASS，5 条。

- [ ] **Step 5: 确认锁真的串行化**

`busy_reason` 的 `with_for_update()` 只有在提交新 Run 的那条路径**也**锁同一行时才起作用。

```bash
grep -n "with_for_update" packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py
```
确认 `submit_end_user_run` 走到的写入路径上锁了 `SessionRow`。**若它没有锁，
把这个事实写进注释——注释不得声称代码没有的保护**，并在计划的遗留问题里记一条。

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/runs/test_withdraw_service.py
git commit -m "test(runs): 撤回撤对了几条，什么时候该拒绝"
git add packages/backend/src/tiny_hermes/runs/domain/models.py \
        packages/backend/src/tiny_hermes/runs/application/service.py \
        packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py \
        packages/backend/src/tiny_hermes/runs/ports/store.py
git commit -m "feat(runs): 把一段历史挡在后续上下文之外"
```

---

### Task 5: 回执——领域文档、两列、扫描器

**Files:**
- Create: `packages/backend/src/tiny_hermes/channels/domain/command_receipt.py`
- Create: `migrations/versions/20260826_0048_command_receipt.py`
- Modify: `packages/backend/src/tiny_hermes/channels/infrastructure/tables.py`（`ChannelEventRow`）
- Modify: `packages/backend/src/tiny_hermes/channels/infrastructure/sql_channel_store.py`
- Test: `packages/backend/tests/unit/channels/test_command_receipt.py`
- Test: `packages/backend/tests/integration/channels/test_command_receipt_scan.py`

**Interfaces:**
- Consumes: Task 1 的 `CommandName`；Task 4 的 `Withdrawal`。
- Produces:
  ```python
  @dataclass(frozen=True)
  class CommandReceipt:
      command: str            # CommandName 的值
      outcome: str            # "done" | "nothing" | "busy"
      messages: int
      turns: int
      echoed_text: str
      busy_reason: str | None # "running" | "queued"
      def document(self) -> dict[str, Any]: ...

  def receipt_from_document(document: dict[str, Any] | None) -> CommandReceipt | None

  # ChannelEventRow
  command_receipt: Mapped[dict[str, Any] | None]
  command_open_id: Mapped[str | None]

  # store
  async def record_command_receipt(
      self, event_row_id: UUID, receipt: CommandReceipt, external_user_id: str
  ) -> None
  async def pending_command_receipts(self, limit: int = 50) -> list[PendingCommandReceipt]

  @dataclass(frozen=True)
  class PendingCommandReceipt:
      event_row_id: UUID
      binding_id: UUID
      receipt: CommandReceipt
      external_user_id: str
  ```

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/channels/test_command_receipt.py
"""回执存的是事实，不是渲染好的句子。

和 BlockedNotice 同一个理由：入站那一刻是唯一准确的时刻，而措辞会变。
存成文档，飞书层渲染。
"""

from tiny_hermes.channels.domain.command_receipt import (
    CommandReceipt,
    receipt_from_document,
)


def test_a_receipt_survives_the_round_trip() -> None:
    receipt = CommandReceipt(
        command="undo",
        outcome="done",
        messages=2,
        turns=1,
        echoed_text="图里是什么",
        busy_reason=None,
    )

    assert receipt_from_document(receipt.document()) == receipt


def test_a_busy_receipt_keeps_which_kind_of_busy() -> None:
    receipt = CommandReceipt(
        command="new",
        outcome="busy",
        messages=0,
        turns=0,
        echoed_text="",
        busy_reason="queued",
    )

    assert receipt_from_document(receipt.document()).busy_reason == "queued"


def test_nothing_is_not_a_receipt() -> None:
    assert receipt_from_document(None) is None
    assert receipt_from_document({}) is None
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_command_receipt.py -q
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现领域文档**

```python
# packages/backend/src/tiny_hermes/channels/domain/command_receipt.py
"""一条命令做成了什么，存成事实而不是句子。

和 `blocked.py` 同一个理由：入站那一刻是唯一准确的时刻。撤回之后队列会变、
措辞会改，但「你刚撤掉了 1 轮、2 条消息」在它发生时是真的，那才是要发给
用户的东西。渲染成飞书里的一句话属于 infrastructure。
"""

from dataclasses import dataclass
from typing import Any

from tiny_hermes.channels.domain._json import int_at, string_at


@dataclass(frozen=True)
class CommandReceipt:
    command: str
    #: `done` | `nothing` | `busy`
    outcome: str
    messages: int
    turns: int
    #: 被撤的那条 user 消息原文。回显它是为了让用户改一改重发，而不是
    #: 让他自己回忆刚才打了什么。
    echoed_text: str
    #: 只在 `outcome == "busy"` 时有值。`running` 与 `queued` 要说不同的话：
    #: 一个是等它跑完，一个是前面还有别的消息排着。
    busy_reason: str | None

    def document(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "outcome": self.outcome,
            "messages": self.messages,
            "turns": self.turns,
            "echoed_text": self.echoed_text,
            "busy_reason": self.busy_reason,
        }


def receipt_from_document(document: dict[str, Any] | None) -> CommandReceipt | None:
    """没有回执和空回执是同一件事：这条事件不是命令。"""
    if not document:
        return None
    command = string_at(document, "command")
    outcome = string_at(document, "outcome")
    if not command or not outcome:
        return None
    return CommandReceipt(
        command=command,
        outcome=outcome,
        messages=int_at(document, "messages", 0),
        turns=int_at(document, "turns", 0),
        echoed_text=string_at(document, "echoed_text") or "",
        busy_reason=string_at(document, "busy_reason") or None,
    )
```

> 签名已核对：`string_at(container, key) -> str | None`，
> `int_at(container, key, default) -> int`（**`default` 是必填位置参数**）。
> 上面的写法与之相符，照抄即可。

- [ ] **Step 4: 加两列与扫描器**

迁移 `20260826_0048_command_receipt.py`，`down_revision = "20260826_0047"`，
加 `channel_events.command_receipt`（`sa.JSON()`，nullable）与
`channel_events.command_open_id`（`sa.String(120)`，nullable）。
docstring 说明为什么是新的一对列而不是复用 `unsupported_kind`/`unsupported_open_id`：
「不支持的消息」和「执行了一条命令」是两件事，共用一列会让扫描器无法区分该说什么。

`sql_channel_store.py` 加 `record_command_receipt` 与 `pending_command_receipts`，
后者照 `pending_refusals` 的形状写——`command_receipt.is_not(None)` 且
`replied_at.is_(None)`，按 `received_at, id` 排序。

- [ ] **Step 5: 写扫描器的集成测试并跑绿**

```python
# packages/backend/tests/integration/channels/test_command_receipt_scan.py
async def test_a_recorded_receipt_is_owed_an_answer(channel_store, claimed_event) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )

    pending = await channel_store.pending_command_receipts()

    assert [p.event_row_id for p in pending] == [claimed_event.id]
    assert pending[0].receipt.messages == 2


async def test_an_answered_receipt_is_not_owed_again(
    channel_store, claimed_event
) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )
    await channel_store.mark_replied(claimed_event.id, note="ok")

    assert await channel_store.pending_command_receipts() == []
```

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_command_receipt.py \
  packages/backend/tests/integration/channels/test_command_receipt_scan.py -q
```
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/unit/channels/test_command_receipt.py \
        packages/backend/tests/integration/channels/test_command_receipt_scan.py
git commit -m "test(channels): 一条命令回执，和它欠谁一句话"
git add packages/backend/src/tiny_hermes/channels/domain/command_receipt.py \
        migrations/versions/20260826_0048_command_receipt.py \
        packages/backend/src/tiny_hermes/channels/infrastructure/tables.py \
        packages/backend/src/tiny_hermes/channels/infrastructure/sql_channel_store.py
git commit -m "feat(channels): 命令回执，存成事实"
```

---

### Task 6: `ChannelIngestion` 接线

**Files:**
- Modify: `packages/backend/src/tiny_hermes/channels/application/ingestion.py`
- Test: `packages/backend/tests/unit/channels/test_command_ingestion.py`

**Interfaces:**
- Consumes: Task 1 `parse`；Task 4 `withdraw_from_session`、`SessionBusy`、`WithdrawScope`；Task 5 `CommandReceipt`。
- Produces: `ChannelIngestion.run_for` 在命令消息上返回 `Delivered(run=None, blocked=None, receipt=CommandReceipt)`。
  `Delivered.run` 因此变为 `AcceptedRun | None`——**所有现有读 `Delivered.run` 的调用方必须处理 `None`**。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/channels/test_command_ingestion.py
"""一条命令走的是另一条路：不建 Run，不进队列，但欠人一句话。"""

import pytest


async def test_a_command_does_not_become_a_run(ingestion, undo_event, binding) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r1"
    )

    assert delivered.run is None
    assert delivered.receipt is not None
    assert delivered.receipt.command == "undo"


async def test_an_ordinary_message_is_untouched(ingestion, text_event, binding) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=text_event, request_id="r2"
    )

    assert delivered.run is not None
    assert delivered.receipt is None


async def test_a_command_from_someone_with_no_conversation_creates_no_session(
    ingestion, undo_event, binding, conversations
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r3"
    )

    assert delivered.receipt.outcome == "nothing"
    assert conversations.created == []


async def test_a_busy_session_gets_a_receipt_that_says_which_kind(
    ingestion, undo_event, binding, busy_coordination
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r4"
    )

    assert delivered.receipt.outcome == "busy"
    assert delivered.receipt.busy_reason == "running"
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_command_ingestion.py -q
```
Expected: FAIL — `Delivered` 没有 `receipt` 字段

- [ ] **Step 3: 实现**

在 `run_for` 里 `upsert_external_identity` 与擦除检查**之后**、`session_for` 之前：

```python
        command = parse(event.text, has_images=bool(event.images))
        if command is not None:
            return await self._command(binding, event, command)
```

`_command` 私有方法：查 `session_for`；没有会话行则回 `outcome="nothing"` 且**不建会话**
（注释写明为什么：一条命令不该把一个从没说过话的人变成一段对话）；
有则按 `command.name` 映射到 `WithdrawScope` 并调 `withdraw_from_session`，
`SessionBusy` 捕获成 `outcome="busy"`，`None` 成 `"nothing"`，否则 `"done"`。

`Delivered` 加 `receipt: CommandReceipt | None = None`，`run` 改为 `AcceptedRun | None`。

- [ ] **Step 4: 修所有读 `Delivered.run` 的调用方**

```bash
grep -rn "\.run\b" packages/backend/src/tiny_hermes/channels | grep -v pycache
```
逐个确认 `None` 分支。**这一步不做，命令会在发送路径上炸成 `AttributeError`。**

- [ ] **Step 5: 跑绿**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels packages/backend/tests/integration/channels -q
```
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/unit/channels/test_command_ingestion.py
git commit -m "test(channels): 命令不该变成一个 Run"
git add packages/backend/src/tiny_hermes/channels/application/ingestion.py
git commit -m "feat(channels): 一条命令走另一条路"
```

---

### Task 7: 飞书渲染，与阻塞卡片里的入口

**Files:**
- Modify: `packages/backend/src/tiny_hermes/channels/infrastructure/feishu_card.py`
- Modify: `packages/backend/src/tiny_hermes/channels/application/outbound.py`（回执的发送分支）
- Test: `packages/backend/tests/unit/channels/test_command_receipt_text.py`
- Test: `packages/backend/tests/integration/channels/test_command_reply_dispatch.py`

**Interfaces:**
- Consumes: Task 5 `CommandReceipt`、`PendingCommandReceipt`。
- Produces: `command_receipt_text(receipt: CommandReceipt) -> str`。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/channels/test_command_receipt_text.py
from tiny_hermes.channels.domain.command_receipt import CommandReceipt
from tiny_hermes.channels.infrastructure.feishu_card import command_receipt_text


def test_a_finished_undo_says_how_much_and_echoes_the_text() -> None:
    text = command_receipt_text(
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None)
    )

    assert "1" in text
    assert "图里是什么" in text


def test_a_busy_receipt_says_which_kind_of_busy() -> None:
    running = command_receipt_text(CommandReceipt("undo", "busy", 0, 0, "", "running"))
    queued = command_receipt_text(CommandReceipt("undo", "busy", 0, 0, "", "queued"))

    assert running != queued


def test_the_blocked_card_names_the_way_out() -> None:
    from tiny_hermes.channels.domain.blocked import BlockedNotice
    from tiny_hermes.channels.infrastructure.feishu_card import blocked_card

    card = blocked_card(
        BlockedNotice(None, "paused", None, None, 2, ("cancel",))
    )

    assert "/new" in str(card)
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_command_receipt_text.py -q
```
Expected: FAIL — `ImportError: cannot import name 'command_receipt_text'`

- [ ] **Step 3: 实现渲染，并在阻塞卡片里写明 `/new`**

```python
#: 回显截断到这里。照上游 Hermes 的 200 字符——够认出是哪条消息，
#: 又不至于把一条长提示整段抄回聊天窗口。
_ECHO_LIMIT = 200


def command_receipt_text(receipt: CommandReceipt) -> str:
    """一条命令的结果，说给发它的人听。"""
    if receipt.outcome == "busy":
        if receipt.busy_reason == "running":
            return "还有一轮在跑。等它结束，或者先取消，再试一次。"
        return "前面还有消息在排队。等队列走完再试一次。"
    if receipt.outcome == "nothing":
        return "没有可撤的内容。"
    if receipt.command == "new":
        return f"已经开始一段新对话。之前的 {receipt.messages} 条消息不再进入上下文。"
    echoed = receipt.echoed_text
    if len(echoed) > _ECHO_LIMIT:
        echoed = echoed[:_ECHO_LIMIT] + "..."
    head = f"已撤回 {receipt.turns} 轮，共 {receipt.messages} 条。"
    return f"{head}\n\n你刚才说的是：\n{echoed}" if echoed else head
```

`_what_you_can_do` 追加一句：

```python
    # §935 要求阻塞卡片给出「新建会话」入口。这个 build 不渲染交互按钮
    # （原因写在本文件 191 行），所以入口是一条命令而不是一个按钮 —— 这句话
    # 就是那个入口本身，删掉它 §935 就没有实现了。
    lines.append("被卡住时，可以发 /new 开始一段新对话。")
```

> `lines` 是该函数内部拼装用的列表名；若现有实现用的是别的名字或直接返回
> 字符串，按现有写法接上去，不要为这一句改函数结构。

- [ ] **Step 4: 接上发送分支**

`outbound.py` 加一条与 `pending_refusals` 平行的分支：取 `pending_command_receipts`，
`binding_target` 拿凭证，`send_text(command_receipt_text(receipt))`，成功后 `mark_replied`。

- [ ] **Step 5: 集成测试——回执真的发出去了**

```python
# packages/backend/tests/integration/channels/test_command_reply_dispatch.py
"""这条是本项目最常见 bug 的直接防线：写进去了不等于有人够得着。"""


async def test_the_receipt_actually_reaches_the_sender(
    outbound, channel_store, claimed_event, sender_spy
) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )

    await outbound.deliver_pending()

    assert sender_spy.texts, "回执被记录了，但没有任何东西把它发出去"
    assert "图里是什么" in sender_spy.texts[0]
```

- [ ] **Step 6: 跑全套并提交**

```bash
uv run --no-sync pytest packages/backend/tests/unit -q
uv run --no-sync pytest packages/backend/tests/integration -q
uv run ruff check packages/backend migrations && uv run pyright
```

```bash
git add packages/backend/tests/unit/channels/test_command_receipt_text.py \
        packages/backend/tests/integration/channels/test_command_reply_dispatch.py
git commit -m "test(channels): 回执要真的到得了发信人手里"
git add packages/backend/src/tiny_hermes/channels/infrastructure/feishu_card.py \
        packages/backend/src/tiny_hermes/channels/application/outbound.py
git commit -m "feat(channels): 回执发得出去，阻塞卡片说得出路"
```

---

## 收尾

- [ ] **真实走一遍。** 部署后在飞书里发 `/undo`，确认机器人回执，再发一条普通消息，
      确认模型看不到被撤的那一轮。**「测试过了」和「这条路走得通」分开写。**
- [ ] **写验收记录** `docs/superpowers/verification/2026-08-26-chat-commands.md`，
      必须有「这一遍没能证明什么」与「不声称什么」两节。
- [ ] **把设计 §8 的三条决定写进产品事实来源**
      `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md`：
      `/undo` 是新的产品面；`/new` 的语义是「同 session 划线」而非新建 Session 实体；
      撤回的消息从 `session.search` 排除。
- [ ] **开 PR，取得真正的 compose-e2e 绿色**，并用
      `gh run view <id> --log | grep "^compose-e2e" | grep -E "passed|✘"` 确认它真的跑了测试。
