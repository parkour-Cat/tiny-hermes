"""Ordering a subject's memories by relevance, in Chinese.

`relevant_in` ranks a subject's remembered facts against the current
request, and that order is not cosmetic: it decides **which memories the
planner sees first, and therefore which survive** when §7.4.2's memory
segment is over budget.

`to_tsvector('simple', …)` does not segment Chinese, so before migration
0045 a Chinese request produced a rank of zero against every row. The
ordering silently degraded to "most recent" — the docstring described a
relevance mechanism that, for this platform's actual users, did not run.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryScope
from tiny_hermes.memory.infrastructure.sql_library import SqlMemoryLibrary
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType


async def _owner(engine: AsyncEngine) -> tuple[UUID, UUID]:
    """An Agent and a subject that already exist, whatever their ids are."""
    async with engine.begin() as connection:
        agent = (
            await connection.execute(text("SELECT id FROM agents LIMIT 1"))
        ).scalar_one()
        subject = (
            await connection.execute(text("SELECT id FROM users LIMIT 1"))
        ).scalar_one()
    return agent, subject


async def _remember(
    engine: AsyncEngine, workspace_id: str, agent_id: UUID, subject: UUID, body: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO memories"
                " (id, workspace_id, agent_id, kind, subject_type, subject_id,"
                "  body, status, origin, created_by, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :w, :a, 'private', 'user', :s,"
                "  :b, 'active', 'proposed', :s, now(), now())"
            ),
            {"w": UUID(workspace_id), "a": agent_id, "s": subject, "b": body},
        )


async def _ordered(
    engine: AsyncEngine, workspace_id: str, agent_id: UUID, subject: UUID, query: str
) -> list[str]:
    scope = MemoryScope(
        workspace_id=UUID(workspace_id),
        agent_id=agent_id,
        kind=MemoryKind.PRIVATE,
        subject=CallerIdentity(caller_type=CallerType.USER, caller_id=subject),
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        found = await SqlMemoryLibrary(db).relevant_in(scope, query, limit=10)
    return [fact.body for fact in found]


async def test_a_chinese_request_ranks_the_memory_that_matches_it_first(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    del published_agent  # it is what puts an Agent in the workspace
    agent_id, subject = await _owner(engine)

    # Written matching-first, so "most recent" would put the *wrong* one on
    # top — which is exactly what happened while every rank was zero.
    await _remember(engine, workspace_id, agent_id, subject, "这个人偏好用中文回复")
    await _remember(engine, workspace_id, agent_id, subject, "服务器磁盘告警阈值是八成")

    ordered = await _ordered(engine, workspace_id, agent_id, subject, "偏好用什么语言")

    assert ordered[0] == "这个人偏好用中文回复"


async def test_an_english_request_still_ranks_correctly(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """The fix adds an index rather than replacing the configuration, and
    this is the assertion that keeps that true."""
    del published_agent
    agent_id, subject = await _owner(engine)

    await _remember(
        engine, workspace_id, agent_id, subject, "prefers replies in English"
    )
    await _remember(
        engine, workspace_id, agent_id, subject, "disk alert threshold is 80 percent"
    )

    ordered = await _ordered(engine, workspace_id, agent_id, subject, "English replies")

    assert ordered[0] == "prefers replies in English"
