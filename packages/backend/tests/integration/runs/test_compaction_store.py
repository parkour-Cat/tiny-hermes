"""压缩摘要要存得住、读得回，而且只保留最新的一份。

只留最新一份是有意的：§7.4.2 让后续压缩更新上一份摘要，所以被读的永远只有
它。原文一条都没删，追溯走 CONTEXT_COMPACTED 事件。
"""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import StoredSummary


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transaction, shared by `store` and the raw count query below.

    `save_summary` and the row count under test have to see each other's
    writes without a commit in between — the same reason
    `test_withdrawal_reach.py`'s `db_session` fixture opens one transaction
    and keeps every call in a test on it.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield session


@pytest.fixture
def store(db_session: AsyncSession) -> SqlRunStore:
    return SqlRunStore(db_session)


@pytest.fixture
def seeded_session(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> tuple[UUID, UUID]:
    """A persistent Session, and the workspace it lives in.

    No Run needed: `session_compactions` only ever references a Session, so
    seeding one through `POST /api/v1/sessions` — the one path allowed to
    assign its `workspace_id` — is all this suite requires.
    """
    session_id = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
    ).json()["id"]
    return UUID(session_id), UUID(scope["X-Workspace-Id"])


async def test_a_summary_comes_back_as_it_went_in(
    store: SqlRunStore, seeded_session: tuple[UUID, UUID]
) -> None:
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


async def test_a_second_summary_replaces_the_first(
    store: SqlRunStore, db_session: AsyncSession, seeded_session: tuple[UUID, UUID]
) -> None:
    session_id, workspace_id = seeded_session
    for last, text_ in ((40, "第一份"), (72, "第二份")):
        await store.save_summary(
            StoredSummary(session_id, 1, last, text_, "model", None, "m"),
            workspace_id=workspace_id,
        )

    found = await store.latest_summary(session_id)

    assert found is not None
    assert found.last_sequence == 72
    assert found.text == "第二份"

    # `latest_summary` alone cannot tell "one row, updated twice" from "two
    # rows, read back with the newest first" — a plain INSERT with no unique
    # constraint would satisfy every assertion above while still keeping a
    # history. Count the table directly, on the same transaction `store`
    # wrote through, rather than inferring the count from the read path.
    row_count = (
        await db_session.execute(
            text("SELECT count(*) FROM session_compactions WHERE session_id = :s"),
            {"s": session_id},
        )
    ).scalar_one()
    assert row_count == 1


async def test_a_session_with_no_compaction_has_no_summary(
    store: SqlRunStore, seeded_session: tuple[UUID, UUID]
) -> None:
    session_id, _ = seeded_session

    assert await store.latest_summary(session_id) is None
