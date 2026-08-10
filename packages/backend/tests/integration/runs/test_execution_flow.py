"""The whole platform, exercised the way it is deployed.

Nothing here reaches past a public route or the two runtimes an operator starts.
One PostgreSQL, one Redis, an API serving real HTTP, a Worker, and a Scheduler:
if an invariant only holds because a test reached into the store directly, it
does not hold here.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import RunCapabilities
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.model import ModelProvider, ModelRequest, ModelResponse
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.runs.ports.store import ClaimedRun, ClaimRunCommand
from tiny_hermes.shared.config import Settings

from integration.support import EventsUrl, ReadStream

PLATFORM = RunCapabilities(can_control=True, can_retry=True)
ROUND_DELAY_MS = 50
LOST_LEASE_SECONDS = 1

RUNNING = text("SELECT id FROM runs WHERE status = 'running'")
LEASED = text("SELECT run_id FROM worker_leases WHERE released_at IS NULL")
TERMINAL_HEADS = text(
    "SELECT count(*) FROM sessions s JOIN runs r ON r.id = s.head_run_id "
    "WHERE r.status IN ('completed', 'failed', 'cancelled')"
)
FINISH_ORDER = text(
    "SELECT session_sequence, status FROM runs "
    "WHERE session_id = :id ORDER BY finished_at, session_sequence"
)
BUDGET = text(
    "SELECT consumed_execution_ms, consumed_model_calls FROM run_budget_scopes "
    "WHERE root_run_id = :id"
)
AUDIT = text("SELECT action, result, resource_id, request_id FROM audit_events")


@dataclass(frozen=True)
class Snapshot:
    """What the platform looked like while one model round was in flight."""

    running: frozenset[UUID]
    leased: frozenset[UUID]
    terminal_heads: int


async def _snapshot(engine: AsyncEngine) -> Snapshot:
    async with engine.connect() as connection:
        running = (await connection.execute(RUNNING)).scalars().all()
        leased = (await connection.execute(LEASED)).scalars().all()
        heads = (await connection.execute(TERMINAL_HEADS)).scalar_one()
    return Snapshot(
        running=frozenset(running), leased=frozenset(leased), terminal_heads=int(heads)
    )


class ObservingModel:
    """A model provider that photographs the platform mid-round.

    The invariants worth proving — one Run executing at a time, a lease held
    only by the Run that is executing, a Session head that is never already
    finished — are only observable while a Run is genuinely in flight, which is
    exactly the window a model call occupies.
    """

    def __init__(self, engine: AsyncEngine, inner: ModelProvider) -> None:
        self._engine = engine
        self._inner = inner
        self.snapshots: list[Snapshot] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.snapshots.append(await _snapshot(self._engine))
        return await self._inner.complete(request)


def _worker(
    engine: AsyncEngine,
    notifier: WakeUpNotifier,
    model: ModelProvider,
    *,
    worker_id: str = "worker-a",
    max_slice_seconds: int = 30,
) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model,
        notifier=notifier,
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=30,
            max_slice_seconds=max_slice_seconds,
            idle_poll_seconds=1,
        ),
    )


def _scheduler(engine: AsyncEngine, notifier: WakeUpNotifier) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=notifier,
        settings=SchedulerSettings(
            max_recovery_attempts=3, event_retention_hours=168, batch_size=50
        ),
    )


async def _audit_rows(engine: AsyncEngine) -> Sequence[Row[Any]]:
    async with engine.connect() as connection:
        return (await connection.execute(AUDIT)).all()


def _transcript(frames: Sequence[httpx.ServerSentEvent]) -> list[str]:
    return [frame.event for frame in frames]


def _assert_contiguous(frames: Sequence[httpx.ServerSentEvent]) -> None:
    """No gap and no repeat: the sequence is the subscriber's only guarantee."""
    sequences = [int(frame.id) for frame in frames]
    assert sequences == list(range(1, len(frames) + 1))


async def test_three_runs_drain_in_session_order_over_one_stack(
    engine: AsyncEngine,
    settings: Settings,
    read_stream: ReadStream,
    events_url: EventsUrl,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    runs = [submit_run(session_id, f"flow-{index}") for index in (1, 2, 3)]
    run_ids = [UUID(str(run["id"])) for run in runs]

    readers = [
        asyncio.create_task(read_stream(events_url(run_id))) for run_id in run_ids
    ]
    await asyncio.sleep(0.3)  # let every subscriber attach before work begins

    notifier = RedisWakeUpNotifier(settings.redis_url)
    model = ObservingModel(engine, DeterministicModelProvider(ROUND_DELAY_MS))
    try:
        # A zero-second slice budget ends every round at a boundary, so the
        # Worker has to re-claim rather than finish a Run in one uninterrupted
        # burst. That is the interleaving a deployed fleet actually produces.
        worker = _worker(engine, notifier, model, max_slice_seconds=0)
        scheduler = _scheduler(engine, notifier)
        while await worker.run_once() is not None:
            await scheduler.run_once()
    finally:
        await notifier.close()

    transcripts = await asyncio.gather(*readers)

    # One Run executes at a time, and only the executing Run holds a lease.
    assert len(model.snapshots) == 6  # three Runs, two rounds each
    for snapshot in model.snapshots:
        assert len(snapshot.running) == 1
        assert snapshot.leased == snapshot.running
        assert snapshot.terminal_heads == 0

    async with engine.connect() as connection:
        finished = (await connection.execute(FINISH_ORDER, {"id": session_id})).all()
    assert [row.session_sequence for row in finished] == [1, 2, 3]
    assert [row.status for row in finished] == ["completed"] * 3

    for frames in transcripts:
        _assert_contiguous(frames)
        assert _transcript(frames) == [
            "run_created",
            "run_lease_acquired",
            "run_slice_ended",
            "run_lease_acquired",
            "run_completed",
        ]

    rows = await _audit_rows(engine)
    assert [row.result for row in rows] == ["succeeded"] * len(rows)
    assert all(row.request_id for row in rows)
    created = [row for row in rows if row.action == "run.created"]
    assert sorted(row.resource_id for row in created) == sorted(run_ids)
    assert len([row for row in rows if row.action == "session.created"]) == 1
    claims = [row for row in rows if row.action == "run.lease_acquired"]
    streamed_claims = sum(
        _transcript(frames).count("run_lease_acquired") for frames in transcripts
    )
    assert len(claims) == streamed_claims


async def _abandon_claim(engine: AsyncEngine) -> ClaimedRun:
    """Claim the Head Run and walk away, exactly as a killed Worker would.

    The lease is left to lapse on its own clock rather than being expired by
    hand, so the Run is genuinely held for longer than a second by a process
    that never executes a round. That is what makes the budget assertion below
    mean something: the platform charges recorded slices, not possession.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="worker-lost",
                lease_seconds=LOST_LEASE_SECONDS,
                request_id="claim-worker-lost",
                capabilities=PLATFORM,
            )
        )
    assert claimed is not None
    await asyncio.sleep(LOST_LEASE_SECONDS + 0.2)
    return claimed


async def test_a_killed_worker_is_recovered_without_losing_or_repeating_work(
    engine: AsyncEngine,
    client: TestClient,
    scope: dict[str, str],
    settings: Settings,
    read_stream: ReadStream,
    events_url: EventsUrl,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    original = submit_run(session_id, "restart-1")
    run_id = UUID(str(original["id"]))

    reader = asyncio.create_task(read_stream(events_url(run_id)))
    await asyncio.sleep(0.3)

    notifier = RedisWakeUpNotifier(settings.redis_url)
    model = ObservingModel(engine, DeterministicModelProvider(ROUND_DELAY_MS))
    try:
        await _abandon_claim(engine)
        await _scheduler(engine, notifier).run_once()
        # A different worker_id, because the point is that the Run survives the
        # process that was holding it rather than waiting for it to come back.
        survivor = _worker(engine, notifier, model, worker_id="worker-b")
        while await survivor.run_once() is not None:
            pass
    finally:
        await notifier.close()

    frames = await reader

    _assert_contiguous(frames)
    assert _transcript(frames) == [
        "run_created",
        "run_lease_acquired",
        "run_interrupted",
        "run_recovery_approved",
        "run_lease_acquired",
        "run_completed",
    ]
    assert len(model.snapshots) == 2  # the survivor ran both rounds, not the first
    assert all(snapshot.terminal_heads == 0 for snapshot in model.snapshots)

    async with engine.connect() as connection:
        budget = (await connection.execute(BUDGET, {"id": run_id})).one()
    # The lost Worker held the Run for over a second without executing a round.
    # The shared budget charges the two rounds the survivor recorded and none of
    # that possession, so it stays far below the time the Run was owned.
    assert budget.consumed_model_calls == 2
    assert 0 < budget.consumed_execution_ms < LOST_LEASE_SECONDS * 1000

    replayed = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "restart-1"},
        json={"session_id": session_id, "input": "message restart-1"},
    )
    assert replayed.status_code == 200
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert replayed.json() == original
