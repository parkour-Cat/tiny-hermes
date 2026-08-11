import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.service import LeaseLost, StateVersionConflict
from tiny_hermes.runs.domain.models import (
    CheckpointEffectStatus,
    RunCapabilities,
    RunSignal,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import (
    ClaimedRun,
    ClaimRunCommand,
    RecordSliceCommand,
    RenewLeaseCommand,
)

FULL = RunCapabilities(can_control=True, can_retry=True)
CHECKPOINT: dict[str, Any] = {"step": "round-1", "kind": "model_call"}


def _factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _claim(
    engine: AsyncEngine, workspace_id: str, worker_id: str = "worker-a", lease: int = 30
) -> ClaimedRun:
    async with _factory(engine).begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=UUID(workspace_id),
                worker_id=worker_id,
                lease_seconds=lease,
                request_id=f"claim-{worker_id}",
                capabilities=FULL,
            )
        )
    assert claimed is not None
    return claimed


def _slice(
    claimed: ClaimedRun,
    workspace_id: str,
    signal: RunSignal | None,
    *,
    lease_version: int = 1,
    executed_ms: int = 1_200,
    model_calls: int = 1,
    tokens: int = 32,
    limit_reached: bool = False,
    assistant_text: str | None = "the answer",
) -> RecordSliceCommand:
    return RecordSliceCommand(
        workspace_id=UUID(workspace_id),
        run_id=claimed.run.id,
        lease_id=claimed.lease_id,
        expected_lease_version=lease_version,
        expected_state_version=claimed.run.state_version,
        signal=signal,
        pause_reason=None,
        limit_reached=limit_reached,
        checkpoint=CHECKPOINT,
        checkpoint_replay_safe=True,
        checkpoint_effect_status=CheckpointEffectStatus.NONE,
        executed_ms=executed_ms,
        model_calls=model_calls,
        tokens=tokens,
        assistant_text=assistant_text,
        request_id="slice-1",
        capabilities=FULL,
    )


async def _row(engine: AsyncEngine, query: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(text(query), params)).one()


async def test_renewing_a_lease_extends_it_and_records_a_heartbeat(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    async with _factory(engine).begin() as session:
        renewed = await SqlRunStore(session).renew_lease(
            RenewLeaseCommand(
                workspace_id=UUID(scope["X-Workspace-Id"]),
                run_id=claimed.run.id,
                lease_id=claimed.lease_id,
                expected_version=1,
                lease_seconds=60,
            )
        )

    assert renewed is not None
    assert renewed.version == 2
    assert renewed.expires_at > claimed.lease_expires_at

    row = await _row(
        engine,
        "SELECT last_heartbeat_at IS NOT NULL FROM runs WHERE id = :id",
        id=claimed.run.id,
    )
    assert row[0] is True


async def test_renewing_a_reclaimed_lease_returns_nothing_and_writes_nothing(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE worker_leases SET released_at = now() WHERE id = :id"),
            {"id": claimed.lease_id},
        )

    async with _factory(engine).begin() as session:
        renewed = await SqlRunStore(session).renew_lease(
            RenewLeaseCommand(
                workspace_id=UUID(scope["X-Workspace-Id"]),
                run_id=claimed.run.id,
                lease_id=claimed.lease_id,
                expected_version=1,
                lease_seconds=60,
            )
        )

    assert renewed is None
    row = await _row(
        engine,
        "SELECT version, last_heartbeat_at FROM worker_leases "
        "JOIN runs ON runs.id = worker_leases.run_id WHERE worker_leases.id = :id",
        id=claimed.lease_id,
    )
    assert row[0] == 1
    assert row[1] is None


async def test_renewing_another_workers_lease_is_refused(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    async with _factory(engine).begin() as session:
        renewed = await SqlRunStore(session).renew_lease(
            RenewLeaseCommand(
                workspace_id=UUID(scope["X-Workspace-Id"]),
                run_id=claimed.run.id,
                lease_id=claimed.lease_id,
                expected_version=99,
                lease_seconds=60,
            )
        )

    assert renewed is None


async def test_a_slice_boundary_requeues_the_run_and_accounts_for_the_work(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    async with _factory(engine).begin() as session:
        snapshot = await SqlRunStore(session).record_slice(
            _slice(claimed, scope["X-Workspace-Id"], RunSignal.SLICE_ENDED)
        )

    assert snapshot.state.value == "queued"
    assert snapshot.state_version == claimed.run.state_version + 1

    run = await _row(
        engine,
        "SELECT status, checkpoint IS NOT NULL, state_version FROM runs WHERE id = :id",
        id=claimed.run.id,
    )
    assert run[0] == "queued"
    assert run[1] is True
    assert run[2] == claimed.run.state_version + 1

    lease = await _row(
        engine,
        "SELECT released_at IS NOT NULL FROM worker_leases WHERE id = :id",
        id=claimed.lease_id,
    )
    assert lease[0] is True

    budget = await _row(
        engine,
        "SELECT consumed_execution_ms, consumed_model_calls, consumed_tokens, version "
        "FROM run_budget_scopes WHERE root_run_id = :id",
        id=claimed.run.budget_root_run_id,
    )
    assert budget[0] == 1_200
    assert budget[1] == 1
    assert budget[2] == 32
    assert budget[3] == 2


async def test_a_completed_slice_releases_the_lease_and_expires_the_key(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    async with _factory(engine).begin() as session:
        snapshot = await SqlRunStore(session).record_slice(
            _slice(claimed, scope["X-Workspace-Id"], RunSignal.COMPLETED)
        )

    assert snapshot.state.value == "completed"
    row = await _row(
        engine,
        "SELECT (SELECT count(*) FROM worker_leases "
        "  WHERE run_id = :id AND released_at IS NULL), "
        "(SELECT count(*) FROM idempotency_records "
        "  WHERE run_id = :id AND expires_at IS NOT NULL), "
        "(SELECT head_run_id FROM sessions WHERE id = :session)",
        id=claimed.run.id,
        session=claimed.run.session_id,
    )
    assert row[0] == 0
    assert row[1] == 1
    assert row[2] is None


async def test_a_limit_pause_writes_the_safety_valve_event_contiguously(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])
    command = _slice(claimed, scope["X-Workspace-Id"], RunSignal.SAFE_PAUSE_REACHED)

    async with _factory(engine).begin() as session:
        from dataclasses import replace

        from tiny_hermes.runs.domain.models import PauseReason

        await SqlRunStore(session).record_slice(
            replace(command, pause_reason=PauseReason.LIMIT, limit_reached=True)
        )

    async with engine.connect() as connection:
        events = (
            await connection.execute(
                text(
                    "SELECT sequence, event_type FROM run_events "
                    "WHERE run_id = :id ORDER BY sequence"
                ),
                {"id": claimed.run.id},
            )
        ).all()

    assert [item[1] for item in events] == [
        "run_created",
        "run_lease_acquired",
        "run_safe_pause_reached",
        "run_limit_reached",
    ]
    assert [item[0] for item in events] == [1, 2, 3, 4]


async def test_a_stale_lease_version_is_refused_and_changes_nothing(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    with pytest.raises(LeaseLost):
        async with _factory(engine).begin() as session:
            await SqlRunStore(session).record_slice(
                _slice(
                    claimed,
                    scope["X-Workspace-Id"],
                    RunSignal.COMPLETED,
                    lease_version=99,
                )
            )

    row = await _row(
        engine,
        "SELECT r.status, r.state_version, b.consumed_execution_ms, "
        "  l.released_at IS NULL "
        "FROM runs r JOIN run_budget_scopes b ON b.root_run_id = r.budget_root_run_id "
        "JOIN worker_leases l ON l.run_id = r.id WHERE r.id = :id",
        id=claimed.run.id,
    )
    assert row[0] == "running"
    assert row[1] == claimed.run.state_version
    assert row[2] == 0
    assert row[3] is True


async def test_a_stale_state_version_is_refused(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])
    from dataclasses import replace

    with pytest.raises(StateVersionConflict):
        async with _factory(engine).begin() as session:
            await SqlRunStore(session).record_slice(
                replace(
                    _slice(claimed, scope["X-Workspace-Id"], RunSignal.COMPLETED),
                    expected_state_version=99,
                )
            )


async def test_execution_time_cannot_be_counted_twice(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    async def attempt() -> object:
        async with _factory(engine).begin() as session:
            return await SqlRunStore(session).record_slice(
                _slice(claimed, scope["X-Workspace-Id"], RunSignal.SLICE_ENDED)
            )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    assert sum(not isinstance(value, BaseException) for value in results) == 1
    budget = await _row(
        engine,
        "SELECT consumed_execution_ms, consumed_model_calls FROM run_budget_scopes "
        "WHERE root_run_id = :id",
        id=claimed.run.budget_root_run_id,
    )
    assert budget[0] == 1_200
    assert budget[1] == 1


async def test_a_slice_that_keeps_the_lease_records_progress_without_a_signal(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    claimed = await _claim(engine, scope["X-Workspace-Id"])

    async with _factory(engine).begin() as session:
        snapshot = await SqlRunStore(session).record_slice(
            _slice(claimed, scope["X-Workspace-Id"], None)
        )

    assert snapshot.state.value == "running"
    assert snapshot.state_version == claimed.run.state_version

    lease = await _row(
        engine,
        "SELECT released_at IS NULL FROM worker_leases WHERE id = :id",
        id=claimed.lease_id,
    )
    budget = await _row(
        engine,
        "SELECT consumed_execution_ms FROM run_budget_scopes WHERE root_run_id = :id",
        id=claimed.run.budget_root_run_id,
    )
    assert lease[0] is True
    assert budget[0] == 1_200
