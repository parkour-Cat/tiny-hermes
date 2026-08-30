"""后面有没有人在等——判定自动续跑该不该让位的那个事实。

参照系是当前 Run 的 `session_sequence`，不是 `head_run_id`：让位是为了让**后面**
那条消息跑起来，而队首是谁与「我后面有没有人」是两件事。
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlRunStore]:
    """A store bound to its own session, the same shape the Worker gets per slice."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield SqlRunStore(session)


def _submit(
    client: TestClient, scope: dict[str, str], session_id: str, key: str
) -> dict[str, object]:
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _run_row(engine: AsyncEngine, run_id: UUID) -> Row[Any]:
    """`id`/`session_sequence` for one Run, read back the way the fixtures below
    hand it to the tests — the tests assert on `.session_sequence` directly.
    """
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT id, session_sequence FROM runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()


@pytest.fixture
async def session_with_one_running_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any]]:
    run = _submit(client, scope, session_id, "only")
    row = await _run_row(engine, UUID(str(run["id"])))
    return UUID(session_id), row


@pytest.fixture
async def session_with_a_queued_run_behind(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any]]:
    head = _submit(client, scope, session_id, "head")
    queued = _submit(client, scope, session_id, "queued")
    head_row = await _run_row(engine, UUID(str(head["id"])))
    queued_row = await _run_row(engine, UUID(str(queued["id"])))
    return UUID(session_id), head_row, queued_row


@pytest.fixture
async def session_with_a_finished_run_behind(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any]]:
    head = _submit(client, scope, session_id, "head")
    finished = _submit(client, scope, session_id, "finished")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": UUID(str(finished["id"]))},
        )
    head_row = await _run_row(engine, UUID(str(head["id"])))
    finished_row = await _run_row(engine, UUID(str(finished["id"])))
    return UUID(session_id), head_row, finished_row


async def test_a_run_alone_in_its_session_has_nobody_waiting(
    store: SqlRunStore, session_with_one_running_run: tuple[UUID, Row[Any]]
) -> None:
    session_id, run = session_with_one_running_run

    assert await store.has_waiting_run(session_id, run.session_sequence) is False


async def test_a_queued_run_behind_it_is_somebody_waiting(
    store: SqlRunStore,
    session_with_a_queued_run_behind: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    session_id, head, _queued = session_with_a_queued_run_behind

    assert await store.has_waiting_run(session_id, head.session_sequence) is True


async def test_a_terminal_run_behind_it_is_not_waiting(
    store: SqlRunStore,
    session_with_a_finished_run_behind: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    session_id, head, _finished = session_with_a_finished_run_behind

    assert await store.has_waiting_run(session_id, head.session_sequence) is False


async def test_a_run_ahead_of_it_does_not_count(
    store: SqlRunStore,
    session_with_a_queued_run_behind: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    session_id, _head, queued = session_with_a_queued_run_behind

    # 从排队那条自己的角度看，它后面没有人。
    assert await store.has_waiting_run(session_id, queued.session_sequence) is False
