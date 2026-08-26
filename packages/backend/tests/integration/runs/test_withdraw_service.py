"""撤回这个操作本身：撤对了几条，什么时候拒绝。

拒绝那条要断言的是**一行都没动**。一个「拒绝了但顺手改了几行」的实现，
测试只看返回值是抓不到的。
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.service import RunCoordination, SessionBusy
from tiny_hermes.runs.domain.models import (
    EndUserEscape,
    RunCapabilities,
    RunState,
    SessionMode,
    WithdrawScope,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore


def _submit(
    client: TestClient, scope: dict[str, str], session_id: str, key: str
) -> dict[str, Any]:
    """Same shape as `test_run_coordination.py`'s own helper — the one path
    allowed to assign a Run's `session_sequence` and its message's own
    `sequence`, so every fixture in this file goes through it for the user
    turn rather than inserting one by hand.
    """
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _finish_with_a_reply(
    engine: AsyncEngine, session_id: str, run: dict[str, Any], reply_text: str
) -> None:
    """Close a Run out by hand.

    Nothing in this suite drives a Run through real execution, so the
    assistant turn and the terminal status are written directly — the same
    shortcut `test_run_coordination.py` takes to simulate "this Run is
    done" (`UPDATE runs SET status = 'completed'`).
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": UUID(str(run["id"]))},
        )
        session_row = (
            await connection.execute(
                text(
                    "SELECT workspace_id, next_message_sequence FROM sessions "
                    "WHERE id = :s"
                ),
                {"s": UUID(session_id)},
            )
        ).one()
        await connection.execute(
            text(
                "INSERT INTO session_messages "
                "(id, session_id, workspace_id, sequence, role, content, "
                "source_run_id, redacted, created_at) "
                "VALUES (:id, :session_id, :workspace_id, :sequence, 'assistant', "
                "CAST(:content AS JSON), :run_id, false, now())"
            ),
            {
                "id": uuid4(),
                "session_id": UUID(session_id),
                "workspace_id": session_row.workspace_id,
                "sequence": session_row.next_message_sequence,
                "content": json.dumps(
                    {"parts": [{"type": "text", "text": reply_text}]}
                ),
                "run_id": UUID(str(run["id"])),
            },
        )
        await connection.execute(
            text(
                "UPDATE sessions SET next_message_sequence = next_message_sequence + 1 "
                "WHERE id = :s"
            ),
            {"s": UUID(session_id)},
        )


async def _message_ids(engine: AsyncEngine, session_id: str) -> list[UUID]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT id FROM session_messages WHERE session_id = :s "
                    "ORDER BY sequence"
                ),
                {"s": UUID(session_id)},
            )
        ).all()
    return [row.id for row in rows]


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlRunStore]:
    """A store bound to its own open transaction.

    `coordination` below wraps this same instance rather than opening a
    second one: the fixtures that seed each test's data commit through
    their own separate connections (ordinary `client.post` requests and
    `engine.begin()` blocks), but the withdrawal itself and the assertions
    that read it back must see each other's writes *before* either commits
    — `test_withdrawing_twice_does_not_move_the_timestamp` calls
    `coordination.withdraw_from_session` twice and reads `store` in between.
    Two independent sessions would not see one another's uncommitted rows.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield SqlRunStore(session)


@pytest.fixture
def coordination(store: SqlRunStore) -> RunCoordination:
    return RunCoordination(store)


@pytest.fixture
async def finished_session_of_four_messages(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, list[UUID]]:
    """A Session with two full exchanges, both Runs already terminal."""
    first = _submit(client, scope, session_id, "undo-first")
    await _finish_with_a_reply(engine, session_id, first, "reply one")
    second = _submit(client, scope, session_id, "undo-second")
    await _finish_with_a_reply(engine, session_id, second, "reply two")

    ids = await _message_ids(engine, session_id)
    assert len(ids) == 4, ids
    return UUID(session_id), ids


@pytest.fixture
async def session_with_a_running_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, list[UUID]]:
    """A Session whose one Run has not reached a terminal state."""
    run = _submit(client, scope, session_id, "undo-busy")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status = 'running' WHERE id = :id"),
            {"id": UUID(str(run["id"]))},
        )

    ids = await _message_ids(engine, session_id)
    assert len(ids) == 1, ids
    return UUID(session_id), ids


@pytest.fixture
async def session_with_a_queued_run_behind_a_finished_head(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, list[UUID]]:
    """未了结的工作**不是**队首。

    §6 明说不得凭 `head_run_id` 一个字段下结论，而在此之前这个文件只造过
    「队首在跑」一种忙。这里队首已经终态、`sessions.head_run_id` 还指着它，
    真正未了结的是排在它后面那一个——`head_run_id` 对它一无所知。
    """
    first = _submit(client, scope, session_id, "queued-head")
    second = _submit(client, scope, session_id, "queued-behind")
    await _finish_with_a_reply(engine, session_id, first, "reply one")

    async with engine.connect() as connection:
        behind = (
            await connection.execute(
                text("SELECT status FROM runs WHERE id = :id"),
                {"id": UUID(str(second["id"]))},
            )
        ).one()
        head = (
            await connection.execute(
                text("SELECT head_run_id FROM sessions WHERE id = :s"),
                {"s": UUID(session_id)},
            )
        ).one()
    assert behind.status == "queued", behind.status
    assert head.head_run_id == UUID(str(first["id"])), head.head_run_id

    return UUID(session_id), await _message_ids(engine, session_id)


@pytest.fixture
async def parked_end_user_session(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> tuple[UUID, UUID, UUID, list[UUID]]:
    """一个停在 `waiting_approval` 上的队首 Run，属于一个终端用户。

    必须是终端用户的 Session 而不是本文件其它 fixture 用的控制台 Session：
    `/new` 取消停住的队首走的是 `cancel_end_user_run`，而它的归属检查读的是
    Session 自己的 `caller`。

    种在一个**单独提交**的事务里——`store` fixture 那个事务全程不提交，而下面
    改状态的 `UPDATE` 走的是另一条连接，看不见没提交的行。
    """
    end_user_id = uuid4()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        seeding = RunCoordination(SqlRunStore(db))
        created = await seeding.create_end_user_session(
            UUID(workspace_id),
            end_user_id,
            UUID(published_agent),
            SessionMode.PERSISTENT,
            "seed",
        )
        accepted = await seeding.submit_end_user_run(
            UUID(workspace_id),
            end_user_id,
            created.id,
            "帮我查一下上周的订单",
            "parked-seed",
            "seed",
        )

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status = 'waiting_approval' WHERE id = :id"),
            {"id": accepted.run_id},
        )

    ids = await _message_ids(engine, str(created.id))
    assert len(ids) == 1, ids
    return created.id, end_user_id, accepted.run_id, ids


async def _state_of(store: SqlRunStore, workspace_id: str, run_id: UUID) -> RunState:
    snapshot = await store.get_run(
        UUID(workspace_id), run_id, RunCapabilities(can_control=True, can_retry=True)
    )
    assert snapshot is not None
    return snapshot.state


async def test_undo_takes_back_the_last_user_turn_and_what_followed(
    coordination: RunCoordination,
    finished_session_of_four_messages: tuple[UUID, list[UUID]],
) -> None:
    # user, assistant, user, assistant
    session_id, _ids = finished_session_of_four_messages

    done = await coordination.withdraw_from_session(
        session_id, WithdrawScope.LAST_EXCHANGE, turns=1
    )

    assert done is not None
    assert done.messages == 2
    assert done.turns == 1


async def test_undo_clamps_when_asked_for_more_turns_than_exist(
    coordination: RunCoordination,
    finished_session_of_four_messages: tuple[UUID, list[UUID]],
) -> None:
    session_id, _ = finished_session_of_four_messages

    done = await coordination.withdraw_from_session(
        session_id, WithdrawScope.LAST_EXCHANGE, turns=99
    )

    assert done is not None
    assert done.turns == 2
    assert done.messages == 4


async def test_new_takes_back_everything(
    coordination: RunCoordination,
    finished_session_of_four_messages: tuple[UUID, list[UUID]],
) -> None:
    session_id, _ = finished_session_of_four_messages

    done = await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert done is not None
    assert done.messages == 4


async def test_a_session_with_work_in_flight_refuses_and_changes_nothing(
    coordination: RunCoordination,
    store: SqlRunStore,
    session_with_a_running_run: tuple[UUID, list[UUID]],
) -> None:
    session_id, ids = session_with_a_running_run

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert raised.value.reason == "running"
    for message_id in ids:
        assert await store.withdrawn_at_of(message_id) is None


async def test_a_run_still_queued_behind_a_finished_head_refuses_as_queued(
    coordination: RunCoordination,
    store: SqlRunStore,
    session_with_a_queued_run_behind_a_finished_head: tuple[UUID, list[UUID]],
) -> None:
    session_id, ids = session_with_a_queued_run_behind_a_finished_head

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert raised.value.reason == "queued"
    for message_id in ids:
        assert await store.withdrawn_at_of(message_id) is None


async def test_new_ends_a_parked_head_run_instead_of_refusing(
    coordination: RunCoordination,
    store: SqlRunStore,
    workspace_id: str,
    parked_end_user_session: tuple[UUID, UUID, UUID, list[UUID]],
) -> None:
    """阻塞卡片写着「被卡住时，可以发 /new 开始一段新对话」。停在等审批上正是
    卡片被渲染出来的那种状态——这里断言那句话是真的。
    """
    session_id, end_user_id, run_id, ids = parked_end_user_session

    done = await coordination.withdraw_from_session(
        session_id,
        WithdrawScope.ALL,
        escape_hatch=EndUserEscape(
            workspace_id=UUID(workspace_id),
            end_user_id=end_user_id,
            request_id="req-new",
        ),
    )

    assert done is not None
    assert done.messages == len(ids)
    # 撤了历史却留着一个还能醒进来的 Run，等于把旧对话接进新对话里。
    assert await _state_of(store, workspace_id, run_id) is RunState.CANCELLED


async def test_undo_refuses_a_parked_head_run_and_leaves_it_running(
    coordination: RunCoordination,
    store: SqlRunStore,
    workspace_id: str,
    parked_end_user_session: tuple[UUID, UUID, UUID, list[UUID]],
) -> None:
    """不对称是故意的：`/new` 是逃生口，有权结束一个停住的 Run；`/undo` 是对
    已经落定的历史动刀，没有理由替用户放弃一个他没说要放弃的 Run。
    """
    session_id, _end_user_id, run_id, ids = parked_end_user_session

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(
            session_id, WithdrawScope.LAST_EXCHANGE, turns=1
        )

    assert raised.value.reason == "parked"
    for message_id in ids:
        assert await store.withdrawn_at_of(message_id) is None
    assert await _state_of(store, workspace_id, run_id) is RunState.WAITING_APPROVAL


async def test_withdrawing_twice_does_not_move_the_timestamp(
    coordination: RunCoordination,
    store: SqlRunStore,
    finished_session_of_four_messages: tuple[UUID, list[UUID]],
) -> None:
    session_id, ids = finished_session_of_four_messages
    await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)
    first = await store.withdrawn_at_of(ids[0])

    again = await coordination.withdraw_from_session(session_id, WithdrawScope.ALL)

    assert again is None
    assert await store.withdrawn_at_of(ids[0]) == first
