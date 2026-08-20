"""A Run that asks to be woken later, and the Scheduler that wakes it.

Design §4.6. `waiting_external` and its wait scan were written in M1 and have
never had a producer, so the whole path has been reachable only through the
signal seam. `platform.wait` is the first producer: the model calls it, the
judge returns `wait`, and `decide_after_round` turns that into
`EXTERNAL_WAIT_STARTED` with `wait_kind="timer"`.

The point of the state is what the Run gives up on the way in. A Run that
waited while holding its lease and its container would be occupying a Worker
slot and a sandbox to do nothing, which is what §12.3 promises it does not do.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    WAIT_SECONDS,
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier

WAITER = ["platform.wait"]


def _worker(engine: AsyncEngine, workspace_id: str) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _scheduler(engine: AsyncEngine) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(
            max_recovery_attempts=3, event_retention_hours=168, batch_size=50
        ),
    )


async def _row(engine: AsyncEngine, run_id: Any) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT status, wait_kind, wait_deadline_at, pause_reason "
                    "FROM runs WHERE id = :id"
                ),
                {"id": UUID(str(run_id))},
            )
        ).one()
    return {
        "status": row[0],
        "wait_kind": row[1],
        "wait_deadline_at": row[2],
        "pause_reason": row[3],
    }


async def _leases(engine: AsyncEngine, run_id: Any) -> int:
    """Leases this Run still holds — a released one has `released_at` set."""
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM worker_leases "
                        "WHERE run_id = :id AND released_at IS NULL"
                    ),
                    {"id": UUID(str(run_id))},
                )
            ).scalar_one()
        )


async def _reach_the_deadline(engine: AsyncEngine, run_id: Any) -> None:
    """Move the deadline into the past instead of sleeping through it.

    The clock is the only thing under test that a test may not wait for. What
    the Scheduler reads is the row, and the row is what this moves.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET wait_deadline_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": UUID(str(run_id))},
        )


async def waiting(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    session_id = session_for(agent_with_scenario("wait_once", tools=WAITER))
    run = submit_run(session_id, "key-1")
    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    return dict(client.get(f"/api/v1/runs/{run['id']}", headers=scope).json())


async def test_a_round_that_asks_to_wait_enters_waiting_external_with_a_deadline(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    assert run["status"] == "waiting_external"
    stored = await _row(engine, run["id"])
    assert stored["wait_kind"] == "timer"
    assert stored["wait_deadline_at"] is not None


async def test_the_deadline_is_the_duration_the_round_asked_for(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """Measured from when the transition was written, not from some other clock.

    A generous window: the assertion is that the deadline came from the model's
    request rather than from a default, not that the two clocks agree to the
    millisecond.
    """
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    deadline = (await _row(engine, run["id"]))["wait_deadline_at"]
    ahead = deadline - datetime.now(UTC)
    assert timedelta(seconds=WAIT_SECONDS - 30) < ahead <= timedelta(
        seconds=WAIT_SECONDS
    )


async def test_a_waiting_run_holds_no_lease(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """The reason the state exists. A Run waiting an hour on a held lease is an
    hour of a Worker slot spent on nothing, and the lease would expire into
    `interrupted` anyway — recovery for a Run that is not in trouble."""
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    assert await _leases(engine, run["id"]) == 0


async def test_a_run_that_only_waits_never_opens_a_sandbox(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """`platform.wait` runs nowhere, so there is nothing to put in a container.

    This is also what keeps §12.3's promise honest at the moment it matters
    most: the Run about to wait is not holding an instance while it waits.
    """
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    async with engine.connect() as connection:
        reserved = (
            await connection.execute(
                text("SELECT count(*) FROM sandbox_reservations WHERE run_id = :id"),
                {"id": UUID(str(run["id"]))},
            )
        ).scalar_one()
    assert reserved == 0


async def test_the_scheduler_puts_it_back_in_the_queue_at_the_deadline(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """A timer's deadline is the wake, not the failure.

    `paused(external_timeout)` is for the kinds whose wake comes from outside —
    an approval nobody gave, a child Run that never finished. Nothing outside
    is late here: the platform itself owns this deadline.
    """
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )
    await _reach_the_deadline(engine, run["id"])

    await _scheduler(engine).run_once()

    stored = await _row(engine, run["id"])
    assert stored["status"] == "queued"
    assert stored["wait_kind"] is None
    assert stored["wait_deadline_at"] is None


async def test_the_woken_run_finishes_the_work_it_stopped_in_the_middle_of(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """Not a new Run and not a replay: the same one, with the wait's own result
    turn in its transcript, which is how the next round knows the wait is
    over."""
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )
    await _reach_the_deadline(engine, run["id"])
    await _scheduler(engine).run_once()

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "completed"


async def test_the_wait_leaves_a_tool_result_in_the_transcript(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """Every call gets a result. A model left without one for a call it made
    will retry it or invent what it returned — and a transcript missing a
    result for a call is malformed for most providers besides."""
    run = await waiting(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT content FROM session_messages WHERE source_run_id = :id "
                "AND role = 'tool' ORDER BY sequence"
            ),
            {"id": UUID(str(run["id"]))},
        )
        contents = [row.content for row in rows.all()]

    assert contents, "the wait's own turn is missing"
    assert "waited" in str(contents[-1])


async def test_an_agent_that_did_not_bind_the_tool_cannot_be_made_to_wait(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """The bound list is the control, not the schema list the model was handed.

    Same scenario, same call, no binding: the call is refused, the round is a
    `continue`, and the Run never reaches `waiting_external`.
    """
    session_id = session_for(agent_with_scenario("wait_once", tools=[]))
    run = submit_run(session_id, "key-1")

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    assert (await _row(engine, run["id"]))["status"] != "waiting_external"
