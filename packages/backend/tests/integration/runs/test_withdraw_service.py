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
from tiny_hermes.runs.domain.models import WithdrawScope
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


async def test_undo_takes_back_the_last_user_turn_and_what_followed(
    coordination: RunCoordination,
    finished_session_of_four_messages: tuple[UUID, list[UUID]],
) -> None:
    session_id, ids = finished_session_of_four_messages   # user, assistant, user, assistant

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
