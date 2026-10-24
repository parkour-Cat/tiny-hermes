"""A published Agent version is read once per process, not once per round.

2026-10-24: a Worker built its context twice a round, and each time read the
version row, parsed its spec again, and looked up again which Agent it
belongs to. A published version never changes (its content hash is fixed)
and a Run's version never changes either, so the answer can be kept.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore


async def test_reading_a_runs_context_twice_reads_its_version_once(
    engine: AsyncEngine, submitted_run: dict[str, Any], workspace_id: str
) -> None:
    run_id = UUID(submitted_run["id"])
    workspace = UUID(workspace_id)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    reads: list[list[str]] = []

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        del conn, cursor, args
        if "FROM agent_versions" in statement:
            reads[-1].append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", watch)
    try:
        for _ in range(2):
            reads.append([])
            async with sessions() as session:
                context = await SqlRunStore(session).execution_context(workspace, run_id)
            assert context is not None
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", watch)

    assert reads[0], "the first read has to fetch the version"
    assert reads[1] == [], f"the second read fetched it again: {reads[1]}"
