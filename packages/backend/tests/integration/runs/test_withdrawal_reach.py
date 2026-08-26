"""撤回之后，还有哪些路看得见它。

Task 2 只管住了 `execution_context`（见 `test_message_withdrawal.py`）。设计
§5.1 的判定表还有四行：`_child_result`、`list_session_messages`、
`_copy_checkpoint_messages`、`sql_search._base`。这里逐条钉住那张表，
不是钉住 `withdrawn_at` 这一列的值——测试打的是每条读路径*交出来*的东西。

会话搜索那条尤其要紧：压缩摘要会主动告诉模型「searchable with
session.search」并附上线索词，被撤的内容若还搜得回来，撤回就是漏的——而这正
是这个功能要修的东西。
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.memory.domain.search import request_for
from tiny_hermes.memory.infrastructure.sql_search import SqlSessionSearch
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transaction, shared by `store` and `search` below.

    `store.mark_withdrawn` and the read under test have to be able to see
    each other's writes without a commit in between — the same reason
    `test_message_withdrawal.py`'s own `store` fixture opens one transaction
    and keeps every call in a test on it.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield session


@pytest.fixture
def store(db_session: AsyncSession) -> SqlRunStore:
    return SqlRunStore(db_session)


@pytest.fixture
def search(db_session: AsyncSession) -> SqlSessionSearch:
    return SqlSessionSearch(db_session)


@pytest.fixture
async def seeded_session_with_two_messages(
    client: TestClient, scope: dict[str, str], published_agent: str, engine: AsyncEngine
) -> tuple[UUID, UUID, UUID]:
    """A persistent Session carrying two ordinary user turns.

    Copied from `test_message_withdrawal.py` rather than imported: pytest
    fixtures defined in one test module are not visible to another, and this
    one's own note on why it goes through `POST /api/v1/runs` rather than
    inserting rows by hand still applies here verbatim.
    """
    session_id = str(
        client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
        ).json()["id"]
    )
    for key in ("reach-first", "reach-second"):
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


@pytest.fixture
async def seeded_session_with_a_searchable_message(
    client: TestClient, scope: dict[str, str], published_agent: str, engine: AsyncEngine
) -> tuple[UUID, UUID]:
    """A Session with one message whose text nothing else in the workspace shares.

    "三视图" is picked as the needle for the same reason
    `test_chinese_relevance.py` picks Chinese text to prove the search: the
    fix lives in `SessionMessageRow.withdrawn_at.is_(None)` inside
    `sql_search._base`, not in whether Chinese matches at all — 0045 already
    covers that. A query only this message can match makes "not found" mean
    "the filter worked", not "the query missed everything".
    """
    session_id = str(
        client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
        ).json()["id"]
    )
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "reach-search"},
        json={"session_id": session_id, "input": "画一张三视图"},
    )
    assert created.status_code == 201, created.text

    async with engine.connect() as connection:
        message_id = (
            await connection.execute(
                text("SELECT id FROM session_messages WHERE session_id = :s"),
                {"s": UUID(session_id)},
            )
        ).scalar_one()
    return UUID(session_id), message_id


@pytest.fixture
async def session_with_a_checkpoint(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, UUID, UUID]:
    """A Run with one message it authorized — `_copy_checkpoint_messages`'s own
    input shape, however it got there.

    Submitted through `POST /api/v1/runs` rather than driven to `failed` and
    actually retried the way `test_run_retry.py` does: `_copy_checkpoint_messages`
    selects by `source_run_id`, never by the source Run's status, so a Run
    that never ran is exactly as good a source as one that failed — and not
    running the Worker keeps this fixture from needing one.
    """
    submitted = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "reach-checkpoint"},
        json={"session_id": session_id, "input": "a message about to be withdrawn"},
    )
    assert submitted.status_code == 201, submitted.text
    run_id = UUID(submitted.json()["id"])

    async with engine.connect() as connection:
        message_id = (
            await connection.execute(
                text("SELECT id FROM session_messages WHERE source_run_id = :r"),
                {"r": run_id},
            )
        ).scalar_one()
    return UUID(session_id), run_id, message_id


@pytest.fixture
async def run_with_two_assistant_turns(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, UUID, UUID]:
    """A Run whose Session holds two assistant turns — `_child_result`'s own
    input shape.

    `_child_result` reads by `session_id` and `role` alone, so this stands in
    for an actual delegated child without driving `agent.delegate` through a
    live model and two Workers the way `test_child_runs.py` does: what is
    under test here is the query's WHERE clause, not §13's delegation path.
    The two assistant rows are inserted directly for the same reason
    `test_hints_are_searchable.py` inserts a `session_messages` row directly
    — nothing in this platform's HTTP surface writes an `assistant` turn on
    demand.
    """
    submitted = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "reach-child-result"},
        json={"session_id": session_id, "input": "do the thing"},
    )
    assert submitted.status_code == 201, submitted.text
    run_id = UUID(submitted.json()["id"])
    workspace_id = UUID(scope["X-Workspace-Id"])

    async with engine.begin() as connection:
        next_sequence = (
            await connection.execute(
                text("SELECT next_message_sequence FROM sessions WHERE id = :s"),
                {"s": UUID(session_id)},
            )
        ).scalar_one()
        earlier_id, later_id = uuid4(), uuid4()
        for offset, message_id, said in (
            (0, earlier_id, "the earlier answer"),
            (1, later_id, "the later answer, about to be withdrawn"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO session_messages"
                    " (id, session_id, workspace_id, sequence, role, content,"
                    "  source_run_id, redacted, created_at)"
                    " VALUES (:id, :s, :w, :seq, 'assistant', :c, :r, false, now())"
                ),
                {
                    "id": message_id,
                    "s": UUID(session_id),
                    "w": workspace_id,
                    "seq": next_sequence + offset,
                    "c": json.dumps(
                        {"role": "assistant", "parts": [{"type": "text", "text": said}]}
                    ),
                    "r": run_id,
                },
            )
        await connection.execute(
            text("UPDATE sessions SET next_message_sequence = :n WHERE id = :s"),
            {"n": next_sequence + 2, "s": UUID(session_id)},
        )
    return run_id, earlier_id, later_id


async def test_session_search_does_not_find_a_withdrawn_message(
    store: SqlRunStore,
    search: SqlSessionSearch,
    scope: dict[str, str],
    seeded_session_with_a_searchable_message: tuple[UUID, UUID],
) -> None:
    _, message_id = seeded_session_with_a_searchable_message
    await store.mark_withdrawn([message_id], at=datetime.now(UTC))

    hits = await search.for_workspace(
        UUID(scope["X-Workspace-Id"]), request_for("三视图")
    )

    assert hits == []


async def test_the_transcript_still_shows_it_and_says_it_was_withdrawn(
    store: SqlRunStore,
    scope: dict[str, str],
    seeded_session_with_two_messages: tuple[UUID, UUID, UUID],
) -> None:
    session_id, _, second_id = seeded_session_with_two_messages
    await store.mark_withdrawn([second_id], at=datetime.now(UTC))

    listed = await store.list_session_messages(
        UUID(scope["X-Workspace-Id"]), session_id
    )

    shown = next(m for m in listed if m.id == second_id)
    assert shown.withdrawn_at is not None


async def test_a_withdrawn_assistant_message_is_not_a_child_run_result(
    store: SqlRunStore,
    db_session: AsyncSession,
    run_with_two_assistant_turns: tuple[UUID, UUID, UUID],
) -> None:
    """A child Run's delegation result takes its most recent assistant turn.
    The one it took back must not be the one that surfaces."""
    run_id, _, later_id = run_with_two_assistant_turns
    await store.mark_withdrawn([later_id], at=datetime.now(UTC))

    run = await db_session.get(RunRow, run_id)
    assert run is not None
    result = await store.child_result_for(run)

    # Asserts the summary landed on the *earlier* turn, not merely that it
    # differs from the withdrawn one — an implementation that dropped the
    # result altogether would also make a bare inequality pass.
    assert result["summary"] == "the earlier answer"


async def test_a_withdrawn_message_is_not_copied_into_a_checkpoint(
    store: SqlRunStore,
    db_session: AsyncSession,
    session_with_a_checkpoint: tuple[UUID, UUID, UUID],
) -> None:
    """A checkpoint is for continuing. Continuing with history the user took
    back is the same as never having taken it back."""
    session_id, run_id, withdrawn_id = session_with_a_checkpoint
    await store.mark_withdrawn([withdrawn_id], at=datetime.now(UTC))

    session_row = await db_session.get(SessionRow, session_id)
    source_row = await db_session.get(RunRow, run_id)
    assert session_row is not None
    assert source_row is not None

    copied = await store.copy_checkpoint_messages(
        session_row, source_row, uuid4(), datetime.now(UTC)
    )

    assert withdrawn_id not in {row.id for row in copied}
