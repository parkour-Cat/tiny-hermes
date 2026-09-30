"""A lease renewal and a slice record for the same Run never deadlock.

A Worker renews its lease from a background task while the round it is
running records a slice — directly, or inside a workspace checkpoint commit,
which does not hold the Worker's in-process `_lease_lock`. On 2026-09-29, at
32 concurrent long tasks, Postgres reported `deadlock detected` between
`renew_lease` (worker_leases, then runs) and `record_slice` (runs, then
worker_leases); the commit lost, the Run was interrupted and then failed.

The interleaving is forced rather than waited for. A third transaction holds
the lease row so that the renewal queues on it first and the slice queues
behind it holding the Run row; releasing it hands the lease to the renewal,
which then needs the Run row — the order the stack hit by chance.
"""

import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    CheckpointEffectStatus,
    RunCapabilities,
    TextBlock,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import (
    ClaimedRun,
    ClaimRunCommand,
    RecordSliceCommand,
    RenewLeaseCommand,
)

FULL = RunCapabilities(can_control=True, can_retry=True)


def _factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _claim(engine: AsyncEngine, workspace_id: str) -> ClaimedRun:
    async with _factory(engine).begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=UUID(workspace_id),
                worker_id="worker-a",
                lease_seconds=30,
                request_id="claim-lock-order",
                capabilities=FULL,
            )
        )
    assert claimed is not None
    return claimed


def _slice(claimed: ClaimedRun, workspace_id: str) -> RecordSliceCommand:
    return RecordSliceCommand(
        workspace_id=UUID(workspace_id),
        run_id=claimed.run.id,
        lease_id=claimed.lease_id,
        expected_state_version=claimed.run.state_version,
        signal=None,
        pause_reason=None,
        limit_reached=False,
        checkpoint={"step": "round-1", "kind": "model_call"},
        checkpoint_replay_safe=True,
        checkpoint_effect_status=CheckpointEffectStatus.NONE,
        executed_ms=1_000,
        model_calls=1,
        tokens=32,
        appended=(CanonicalMessage("assistant", (TextBlock(text="round one"),)),),
        request_id="slice-lock-order",
        capabilities=FULL,
    )


async def _waiting_on_locks(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        ).one()
    return int(row[0])


async def _until_waiting(engine: AsyncEngine, count: int) -> None:
    for _ in range(200):
        if await _waiting_on_locks(engine) >= count:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"never saw {count} transactions waiting on a lock")


async def test_a_renewal_and_a_slice_record_for_one_run_both_complete(
    submitted_run: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    del submitted_run
    workspace_id = scope["X-Workspace-Id"]
    claimed = await _claim(engine, workspace_id)
    factory = _factory(engine)

    async def renew() -> Any:
        async with factory.begin() as session:
            return await SqlRunStore(session).renew_lease(
                RenewLeaseCommand(
                    workspace_id=UUID(workspace_id),
                    run_id=claimed.run.id,
                    lease_id=claimed.lease_id,
                    expected_version=1,
                    lease_seconds=60,
                )
            )

    async def record() -> Any:
        async with factory.begin() as session:
            return await SqlRunStore(session).record_slice(_slice(claimed, workspace_id))

    async with factory.begin() as gate:
        await gate.execute(
            text("SELECT id FROM worker_leases WHERE id = :id FOR UPDATE"),
            {"id": claimed.lease_id},
        )
        renewing = asyncio.create_task(renew())
        await _until_waiting(engine, 1)
        recording = asyncio.create_task(record())
        await _until_waiting(engine, 2)
    # The gate has committed: the lease goes to whoever queued on it first.

    renewed, recorded = await asyncio.wait_for(
        asyncio.gather(renewing, recording, return_exceptions=True), timeout=15
    )

    assert not isinstance(renewed, BaseException), f"the renewal failed: {renewed!r}"
    assert not isinstance(recorded, BaseException), f"the slice record failed: {recorded!r}"
