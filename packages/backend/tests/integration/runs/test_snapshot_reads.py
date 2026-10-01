"""A Run snapshot reads its Session's live Runs, not the Session's history.

Every snapshot — each Run read through the API, each claim, each recorded
slice — computed the queue position from every Run the Session ever had, though
only the unfinished ones decide it. A long-lived Session, such as one person's
chat with the bot, grows by a Run per message; on 2026-10-01 a Session holding
300,000 finished Runs made that one read take 98 ms.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.domain.models import RunCapabilities
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore

FULL = RunCapabilities(can_control=True, can_retry=True)


async def _rows_returned_from_runs(engine: AsyncEngine, workspace_id: str, run_id: str) -> int:
    """Rows the snapshot's own statements bring back from `runs`, as the plan counts them."""
    sent: list[tuple[str, Any]] = []

    def capture(_c: Any, _cur: Any, statement: str, parameters: Any, _x: Any, _m: Any) -> None:
        sent.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with async_sessionmaker(engine)() as session:
            snapshot = await SqlRunStore(session).get_run(UUID(workspace_id), UUID(run_id), FULL)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert snapshot is not None
    returned = 0
    async with engine.connect() as connection:
        for statement, parameters in sent:
            if not statement.lstrip().upper().startswith("SELECT") or "FROM runs" not in statement:
                continue
            plan = await connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
            )
            document = plan.scalar_one()
            returned += int(document[0]["Plan"]["Actual Rows"])
    return returned


async def test_a_snapshot_does_not_fetch_the_sessions_finished_runs(
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session = session_for(agent_with_scenario("complete"))
    finished = [str(submit_run(session, f"done-{n}")["id"]) for n in range(60)]
    live = str(submit_run(session, "live-1")["id"])
    str(submit_run(session, "live-2")["id"])
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status = 'completed', finished_at = now() WHERE id = ANY(:ids)"),
            {"ids": [UUID(run) for run in finished]},
        )
        await connection.execute(
            text("UPDATE sessions SET head_run_id = :head WHERE id = :id"),
            {"head": UUID(live), "id": UUID(session)},
        )
        await connection.execute(
            text("UPDATE runs SET blocked_by_run_id = NULL WHERE id = :id"), {"id": UUID(live)}
        )

    assert await _rows_returned_from_runs(engine, scope["X-Workspace-Id"], live) <= 5
