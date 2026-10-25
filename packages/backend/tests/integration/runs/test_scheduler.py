import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
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


class _LeaseScanBroken(SchedulerRuntime):
    """A scan that throws every cycle, as one bad row would make it."""

    async def _reclaim_expired_leases(self, now: datetime) -> None:
        raise RuntimeError("lease scan cannot read this row")


async def test_a_failing_scan_does_not_starve_the_scans_after_it(
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """The lease scan runs first and retention near the end. If the first
    always throws and that ends the cycle, nothing after it ever runs again:
    recovery, approval expiry and cleanup stop, and the log shows one error
    repeating."""
    submit_run(session_for(agent_with_scenario("complete")), "key-1")
    await _worker(engine).run_once()
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE idempotency_records SET expires_at = now() - interval '1 hour'")
        )
    scheduler = _LeaseScanBroken(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(
            max_recovery_attempts=3, event_retention_hours=168, batch_size=50
        ),
    )

    # Still raised, after the others ran: a broken scan must not pass quietly.
    with pytest.raises(ExceptionGroup) as raised:
        await scheduler.run_once()

    assert await _counts(engine, "SELECT count(*) FROM idempotency_records") == 0
    [failure] = raised.value.exceptions
    assert "lease scan cannot read this row" in str(failure)


async def test_login_failures_outside_the_throttle_window_are_deleted(
    engine: AsyncEngine,
) -> None:
    """They decide nothing any more (§23.16) and would otherwise pile up."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO login_failures (subject_key, failed_at) VALUES "
                "('old', now() - interval '2 hours'), ('recent', now())"
            )
        )

    await _scheduler(engine).run_once()

    async with engine.connect() as connection:
        kept = (await connection.execute(text("SELECT subject_key FROM login_failures"))).scalars()
        assert list(kept) == ["recent"]


async def test_housekeeping_runs_once_a_minute_while_the_rest_run_every_pass(
    engine: AsyncEngine, empty_database: None
) -> None:
    """2026-10-24: an idle scheduler made 52 statements a second, a pass every
    1.7 s. Deleting expired records, upload garbage and old artifacts, and
    cancelling 24-hour compat timeouts, that often is waste; recovery, waits
    and approvals are latency and stay on every pass."""
    del empty_database
    clock = [1_000.0]
    runtime = SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(
            max_recovery_attempts=3, event_retention_hours=168, housekeeping_seconds=60
        ),
        clock=lambda: clock[0],
    )
    ran: Counter[str] = Counter()

    def counted(name: str, scan: Any) -> Any:
        async def wrapped(*args: Any) -> None:
            ran[name] += 1
            await scan(*args)

        return wrapped

    for name in (
        "_collect_expired_records",
        "_collect_upload_garbage",
        "_expire_artifacts",
        "_cancel_aged_compat_timeouts",
        "_recover_interrupted",
        "_expire_approvals",
    ):
        setattr(runtime, name, counted(name, getattr(runtime, name)))

    await runtime.run_once()
    await runtime.run_once()

    assert ran["_collect_expired_records"] == 1, "housekeeping runs on the first pass only"
    assert ran["_collect_upload_garbage"] == 1
    assert ran["_expire_artifacts"] == 1
    assert ran["_cancel_aged_compat_timeouts"] == 1
    assert ran["_recover_interrupted"] == 2, "latency scans run on every pass"
    assert ran["_expire_approvals"] == 2

    clock[0] += 61
    await runtime.run_once()

    assert ran["_collect_expired_records"] == 2
    assert ran["_cancel_aged_compat_timeouts"] == 2


def _runtime(engine: AsyncEngine, **settings: Any) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=168, **settings),
    )


def _wrap(runtime: SchedulerRuntime, name: str, before: Any) -> None:
    scan = getattr(runtime, name)

    async def wrapped(*args: Any) -> None:
        await before(name)
        await scan(*args)

    setattr(runtime, name, wrapped)


async def test_two_slow_scans_do_not_wait_for_each_other(
    engine: AsyncEngine, empty_database: None
) -> None:
    """2026-10-24: the scans ran one after another, so any slow one held up
    every scan behind it — reply delivery, approval expiry, due waits."""
    del empty_database
    runtime = _runtime(engine)

    async def slow(_: str) -> None:
        await asyncio.sleep(1.0)

    _wrap(runtime, "_expire_approvals", slow)
    _wrap(runtime, "_settle_child_waits", slow)
    started = asyncio.get_running_loop().time()

    await runtime.run_once()

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.8, f"{elapsed:.2f}s: the slow scans ran in turn"


async def test_the_recovery_chain_keeps_its_order(
    engine: AsyncEngine, empty_database: None
) -> None:
    """Sandboxes are cleaned before their Runs are recovered, so a new Worker
    does not meet a reservation the cleanup has not reached yet."""
    del empty_database
    runtime = _runtime(engine)
    order: list[str] = []

    async def note(name: str) -> None:
        order.append(name)
        await asyncio.sleep(0.05)

    chain = (
        "_reclaim_expired_leases",
        "_isolate_interrupted_sandboxes",
        "_reclaim_sandboxes",
        "_recover_interrupted",
    )
    for name in chain:
        _wrap(runtime, name, note)

    await runtime.run_once()

    assert order == list(chain)


async def test_a_scan_that_hangs_is_abandoned_and_reported_without_holding_the_rest(
    engine: AsyncEngine, empty_database: None
) -> None:
    del empty_database
    # Two seconds, not half of one: the timeout applies to every scan, and
    # on a loaded CI runner (2026-10-25) a real scan took longer than 0.5 s
    # and was reported alongside the hung one.
    runtime = _runtime(engine, scan_timeout_seconds=2.0)
    ran: list[str] = []

    async def hang(_: str) -> None:
        await asyncio.sleep(30)

    async def note(name: str) -> None:
        ran.append(name)

    _wrap(runtime, "_dispatch_channel_replies", hang)
    _wrap(runtime, "_expire_approvals", note)
    _wrap(runtime, "_recover_interrupted", note)
    started = asyncio.get_running_loop().time()

    with pytest.raises(ExceptionGroup) as failed:
        await runtime.run_once()

    assert asyncio.get_running_loop().time() - started < 8.0
    assert sorted(ran) == ["_expire_approvals", "_recover_interrupted"]
    [timeout] = [
        error
        for error in failed.value.exceptions
        if "dispatch_channel_replies" in " ".join(getattr(error, "__notes__", []))
    ]
    assert isinstance(timeout, TimeoutError)
