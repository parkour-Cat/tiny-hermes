"""The Scheduler's head scan: which Sessions break the FIFO invariant (§11.1).

The corruption cases pin what the scan finds, so rewriting it for speed cannot
change the answer. The last test pins the speed: on 2026-09-30 the scan took
4 s over 7,534 Sessions — one sequential pass over `runs` for every Session,
finished or not — and the Scheduler ran it every second, ahead of wait
deadlines, approval expiry and channel replies.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any, cast
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore

Submit = Callable[[str, str], dict[str, Any]]


async def _flagged(engine: AsyncEngine) -> set[str]:
    async with async_sessionmaker(engine)() as session:
        return {str(found) for found in await SqlRunStore(session).sessions_needing_repair(100)}


async def _sql(engine: AsyncEngine, statement: str, **values: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(statement), values)


def _two_runs(
    session_for: Callable[[str], str], agent: str, submit_run: Submit
) -> tuple[str, str, str]:
    session = session_for(agent)
    first = str(submit_run(session, f"{session}-1")["id"])
    second = str(submit_run(session, f"{session}-2")["id"])
    return session, first, second


async def test_healthy_empty_and_finished_sessions_are_not_flagged(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    agent = agent_with_scenario("complete")
    _two_runs(session_for, agent, submit_run)
    session_for(agent)
    finished = session_for(agent)
    run = str(submit_run(finished, "finished-1")["id"])
    await _sql(
        engine,
        "UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :id",
        id=UUID(run),
    )
    await _sql(engine, "UPDATE sessions SET head_run_id = NULL WHERE id = :id", id=UUID(finished))

    assert await _flagged(engine) == set()


async def test_a_session_with_live_runs_and_no_head_is_flagged(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    session, _, _ = _two_runs(session_for, agent_with_scenario("complete"), submit_run)
    await _sql(engine, "UPDATE sessions SET head_run_id = NULL WHERE id = :id", id=UUID(session))

    assert await _flagged(engine) == {session}


async def test_a_head_left_on_the_last_finished_run_is_flagged(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    session = session_for(agent_with_scenario("complete"))
    run = str(submit_run(session, "only-1")["id"])
    await _sql(
        engine,
        "UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :id",
        id=UUID(run),
    )

    assert await _flagged(engine) == {session}


async def test_a_head_on_a_finished_run_ahead_of_live_ones_is_flagged(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    session, first, _ = _two_runs(session_for, agent_with_scenario("complete"), submit_run)
    await _sql(
        engine,
        "UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :id",
        id=UUID(first),
    )

    assert await _flagged(engine) == {session}


async def test_a_head_that_is_not_the_smallest_live_sequence_is_flagged(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    session, _, second = _two_runs(session_for, agent_with_scenario("complete"), submit_run)
    await _sql(
        engine,
        "UPDATE sessions SET head_run_id = :head WHERE id = :id",
        head=UUID(second),
        id=UUID(session),
    )

    assert await _flagged(engine) == {session}


async def test_a_pending_run_not_blocked_by_the_head_is_flagged(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    session, _, second = _two_runs(session_for, agent_with_scenario("complete"), submit_run)
    await _sql(engine, "UPDATE runs SET blocked_by_run_id = NULL WHERE id = :id", id=UUID(second))

    assert await _flagged(engine) == {session}


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


async def _executions_over_runs(engine: AsyncEngine) -> int:
    """The most times any one plan node read `runs`, from the scan's real SQL."""
    sent: list[tuple[str, Any]] = []

    def capture(_c: Any, _cur: Any, statement: str, parameters: Any, _x: Any, _m: Any) -> None:
        sent.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await _flagged(engine)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    statement, parameters = sent[-1]
    async with engine.connect() as connection:
        raw = await connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
        )
        plan = raw.scalar_one()
    document = cast(list[dict[str, Any]], plan if isinstance(plan, list) else json.loads(plan))
    return max(
        (
            int(node["Actual Loops"])
            for node in _nodes(document[0]["Plan"])
            if node.get("Relation Name") == "runs"
        ),
        default=0,
    )


async def test_the_scan_does_not_read_runs_once_per_session_without_live_work(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Submit,
) -> None:
    agent = agent_with_scenario("complete")
    for _ in range(200):
        session_for(agent)
    _two_runs(session_for, agent, submit_run)

    assert await _executions_over_runs(engine) <= 5
