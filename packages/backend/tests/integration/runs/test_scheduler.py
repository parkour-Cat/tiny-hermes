import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import RunCapabilities
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import ClaimedRun, ClaimRunCommand

FULL = RunCapabilities(can_control=True, can_retry=True)


def _scheduler(
    engine: AsyncEngine,
    *,
    max_recovery_attempts: int = 3,
    event_retention_hours: int = 168,
) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(
            max_recovery_attempts=max_recovery_attempts,
            event_retention_hours=event_retention_hours,
            batch_size=50,
        ),
    )


def _worker(engine: AsyncEngine, worker_id: str = "worker-a") -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    )


async def _abandon_claim(engine: AsyncEngine, lease_seconds: int = 1) -> ClaimedRun:
    """Claim a Run and walk away, exactly as a killed Worker would."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="doomed-worker",
                lease_seconds=lease_seconds,
                request_id="claim-doomed",
                capabilities=FULL,
            )
        )
    assert claimed is not None
    return claimed


async def _expire_lease(engine: AsyncEngine, run_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE worker_leases SET expires_at = now() - interval '1 minute' "
                "WHERE run_id = :id"
            ),
            {"id": run_id},
        )


async def _run_row(engine: AsyncEngine, run_id: UUID) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT status, recovery_attempts, state_version FROM runs "
                    "WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()


async def _counts(engine: AsyncEngine, query: str, **params: Any) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(text(query), params)).scalar_one())


async def test_an_abandoned_lease_becomes_interrupted_then_queued(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)
    assert submitted_run["id"]

    await _scheduler(engine).run_once()

    status, attempts, _ = await _run_row(engine, claimed.run.id)
    assert status == "queued"
    assert attempts == 1
    events = await _counts(
        engine,
        "SELECT count(*) FROM run_events WHERE run_id = :id AND event_type IN "
        "('run_interrupted', 'run_recovery_approved')",
        id=claimed.run.id,
    )
    assert events == 2
    open_leases = await _counts(
        engine,
        "SELECT count(*) FROM worker_leases WHERE run_id = :id AND released_at IS NULL",
        id=claimed.run.id,
    )
    assert open_leases == 0


async def test_a_recovered_run_is_finished_by_another_worker(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("complete"))
    run = submit_run(session_id, "key-1")
    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)

    await _scheduler(engine).run_once()
    assert await _worker(engine, "worker-b").run_once() == UUID(str(run["id"]))

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "completed"

    async with engine.connect() as connection:
        sequences = list(
            (
                await connection.execute(
                    text(
                        "SELECT sequence FROM run_events WHERE run_id = :id "
                        "ORDER BY sequence"
                    ),
                    {"id": UUID(str(run["id"]))},
                )
            )
            .scalars()
            .all()
        )
    assert sequences == list(range(1, len(sequences) + 1))


async def test_an_unsafe_checkpoint_fails_instead_of_looping(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET checkpoint_replay_safe = false WHERE id = :id"),
            {"id": claimed.run.id},
        )
    assert submitted_run["id"]

    await _scheduler(engine).run_once()

    status, attempts, _ = await _run_row(engine, claimed.run.id)
    assert status == "failed"
    assert attempts == 0


async def test_an_unknown_external_effect_fails_instead_of_recovering(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET checkpoint_effect_status = 'unknown' WHERE id = :id"),
            {"id": claimed.run.id},
        )
    assert submitted_run["id"]

    await _scheduler(engine).run_once()

    status, _, _ = await _run_row(engine, claimed.run.id)
    assert status == "failed"


async def test_recovery_stops_at_the_attempt_ceiling(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    assert submitted_run["id"]
    scheduler = _scheduler(engine, max_recovery_attempts=2)

    for expected in (1, 2):
        claimed = await _abandon_claim(engine)
        await _expire_lease(engine, claimed.run.id)
        await scheduler.run_once()
        status, attempts, _ = await _run_row(engine, claimed.run.id)
        assert status == "queued"
        assert attempts == expected

    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)
    await scheduler.run_once()

    status, attempts, _ = await _run_row(engine, claimed.run.id)
    assert status == "failed"
    assert attempts == 2


async def test_expired_idempotency_records_are_deleted(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("complete"))
    submit_run(session_id, "key-1")
    await _worker(engine).run_once()
    assert await _counts(
        engine, "SELECT count(*) FROM idempotency_records WHERE expires_at IS NOT NULL"
    ) == 1
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE idempotency_records SET expires_at = now() - interval '1 hour'"
            )
        )

    await _scheduler(engine).run_once()

    assert await _counts(engine, "SELECT count(*) FROM idempotency_records") == 0
    # Removing the key must not remove the Run it once identified.
    listed = client.get(f"/api/v1/runs?session_id={session_id}", headers=scope)
    assert listed.status_code == 200
    assert [item["status"] for item in listed.json()] == ["completed"]


async def test_only_terminal_run_events_outside_retention_are_pruned(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("complete"))
    finished = submit_run(session_id, "key-1")
    await _worker(engine).run_once()
    pending = submit_run(session_id, "key-2")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE run_events SET occurred_at = now() - interval '30 days'")
        )

    await _scheduler(engine, event_retention_hours=1).run_once()

    assert await _counts(
        engine,
        "SELECT count(*) FROM run_events WHERE run_id = :id",
        id=UUID(str(finished["id"])),
    ) == 0
    assert await _counts(
        engine,
        "SELECT count(*) FROM run_events WHERE run_id = :id",
        id=UUID(str(pending["id"])),
    ) == 1


async def test_a_corrupted_session_head_is_repaired_and_audited_once(
    engine: AsyncEngine,
    session_id: str,
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    first = submit_run(session_id, "key-1")
    submit_run(session_id, "key-2")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": UUID(str(first["id"]))},
        )

    await _scheduler(engine).run_once()

    assert await _counts(
        engine,
        "SELECT count(*) FROM audit_events WHERE action = 'session.head_repaired'",
    ) == 1


async def test_a_second_cycle_changes_nothing(
    engine: AsyncEngine,
    session_id: str,
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    submit_run(session_id, "key-1")
    submit_run(session_id, "key-2")
    scheduler = _scheduler(engine)

    await scheduler.run_once()
    await scheduler.run_once()

    assert await _counts(
        engine,
        "SELECT count(*) FROM audit_events WHERE action = 'session.head_repaired'",
    ) == 0
    assert await _counts(
        engine,
        "SELECT count(*) FROM run_events WHERE event_type = 'session_head_repaired'",
    ) == 0


async def test_two_schedulers_produce_one_repair(
    engine: AsyncEngine,
    session_id: str,
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    first = submit_run(session_id, "key-1")
    submit_run(session_id, "key-2")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": UUID(str(first["id"]))},
        )

    results = await asyncio.gather(
        _scheduler(engine).run_once(),
        _scheduler(engine).run_once(),
        return_exceptions=True,
    )

    assert [value for value in results if isinstance(value, BaseException)] == []
    assert await _counts(
        engine,
        "SELECT count(*) FROM audit_events WHERE action = 'session.head_repaired'",
    ) == 1


async def test_a_passed_wait_deadline_pauses_an_externally_waiting_run(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    """Written and tested now, dormant until phase 3 can produce such a Run."""
    run_id = UUID(str(submitted_run["id"]))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'waiting_external', wait_kind = 'child_runs', "
                "wait_deadline_at = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": run_id},
        )

    await _scheduler(engine).run_once()

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT status, pause_reason FROM runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()
    assert row[0] == "paused"
    assert row[1] == "external_timeout"


async def test_a_compat_timeout_pause_older_than_a_day_is_cancelled(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "compat-age"},
        json={"session_id": session_id, "input": "age out"},
    ).json()
    paused = client.post(
        f"/api/v1/runs/{created['id']}/pause",
        headers=scope,
        json={"expected_state_version": created["state_version"]},
    )
    assert paused.status_code == 200
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET pause_reason = 'compat_timeout', "
                "updated_at = now() - interval '25 hours' WHERE id = :id"
            ),
            {"id": created["id"]},
        )

    await _scheduler(engine).run_once()

    aged = client.get(f"/api/v1/runs/{created['id']}", headers=scope).json()
    assert aged["status"] == "cancelled"
