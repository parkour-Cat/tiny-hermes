"""压缩摘要要存得住、读得回，而且只保留最新的一份。

只留最新一份是有意的：§7.4.2 让后续压缩更新上一份摘要，所以被读的永远只有
它。原文一条都没删，追溯走 CONTEXT_COMPACTED 事件。
"""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import StoredSummary


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlRunStore]:
    """A store bound to its own session, the same shape the Worker gets per slice."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield SqlRunStore(session)


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
