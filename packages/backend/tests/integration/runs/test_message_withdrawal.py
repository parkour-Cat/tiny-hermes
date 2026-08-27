"""撤回的消息不进模型上下文——查的是上下文,不是数据库。

这个项目最常见的 bug 是「写进去了不等于有人够得着」。它的镜像同样成立:
把一行标记成已撤回,不等于每一条读它的路都看不到。所以这里断言的是
`execution_context` 交出来的 history,而不是那一列的值。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlRunStore]:
    """A store bound to its own session, the same shape the Worker gets per slice."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield SqlRunStore(session)


@pytest.fixture
async def seeded_session_with_two_messages(
    client: TestClient, scope: dict[str, str], published_agent: str, engine: AsyncEngine
) -> tuple[UUID, UUID, UUID]:
    """A persistent Session carrying two ordinary user turns.

    Seeded through `POST /api/v1/runs`, the same path every other test in this
    directory uses — that request is the one place allowed to assign a Run's
    `session_sequence`, its own budget scope and its pinned Agent Version.
    Reproducing those invariants by hand here would just be a second copy of
    `accept_run` to keep in sync with the first.
    """
    session_id = str(
        client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
        ).json()["id"]
    )
    for key in ("withdrawal-first", "withdrawal-second"):
        created = client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": key},
            json={"session_id": session_id, "input": f"message {key}"},
        )
        assert created.status_code == 201, created.text

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
    assert len(rows) == 2, rows
    return UUID(session_id), rows[0].id, rows[1].id


async def _a_new_run_in(engine: AsyncEngine, session_id: UUID) -> tuple[UUID, UUID]:
    """`(workspace_id, run_id)` of the Run whose next request this session would send.

    Reuses the most recently accepted Run in the session rather than inserting
    a bare `RunRow` here: a persistent Session hands every Run its whole
    history regardless of which Run said what (see `execution_context`'s own
    comment on `session_mode`), so the already-seeded second Run already is
    "the run about to build its next request" — and building one by hand would
    have to re-derive `accept_run`'s invariants (its unique `session_sequence`,
    its own budget scope, its pinned Agent Version) outside the one path that
    is allowed to enforce them.
    """
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT id, workspace_id FROM runs WHERE session_id = :s "
                    "ORDER BY session_sequence DESC LIMIT 1"
                ),
                {"s": session_id},
            )
        ).one()
    return row.workspace_id, row.id


async def test_a_withdrawn_message_is_not_in_the_next_request(
    store: SqlRunStore,
    engine: AsyncEngine,
    seeded_session_with_two_messages: tuple[UUID, UUID, UUID],
) -> None:
    session_id, first_id, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    workspace_id, run_id = await _a_new_run_in(engine, session_id)
    context = await store.execution_context(workspace_id, run_id)

    assert context is not None
    assert [m.id for m in context.history] == [first_id]


async def test_the_withdrawn_row_is_still_in_the_database(
    store: SqlRunStore,
    seeded_session_with_two_messages: tuple[UUID, UUID, UUID],
) -> None:
    _, _, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    assert await store.withdrawn_at_of(second_id) is not None
