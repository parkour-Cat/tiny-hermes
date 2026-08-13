"""The store-level guard on the `interrupted -> paused(limit)` door.

Design §6.3: the rollback transaction records where the Run must go and which
sandbox must be confirmed gone; the Scheduler clears those columns only in the
same transition that reaches the target. The state machine allows the signal —
this guard is what makes it about *this* Run's recorded cleanup rather than
any interrupted Run somebody points at.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    CheckpointEffectStatus,
    PauseReason,
    RunCapabilities,
    RunSignal,
    RunState,
    TextBlock,
    WorkspaceCleanupTarget,
)
from tiny_hermes.runs.domain.state_machine import InvalidStateMetadata
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.runs.ports.store import (
    ApplySignalCommand,
    ClaimRunCommand,
    RecordSliceCommand,
)

PLATFORM = RunCapabilities(can_control=True, can_retry=True)
SANDBOX = uuid.uuid4()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def claimed(
    sessions: async_sessionmaker[AsyncSession], submitted_run: dict[str, Any]
) -> Any:
    del submitted_run
    async with sessions.begin() as db:
        claim = await SqlRunStore(db).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="workspace-guard",
                lease_seconds=60,
                request_id="claim-workspace-guard",
                capabilities=PLATFORM,
            )
        )
    assert claim is not None
    return claim


async def _interrupt_with_intent(
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
    *,
    target: WorkspaceCleanupTarget | None,
) -> None:
    """The rollback transaction: intent and interruption written together."""
    async with sessions.begin() as db:
        await SqlRunStore(db).record_slice(
            RecordSliceCommand(
                workspace_id=claimed.run.workspace_id,
                run_id=claimed.run.id,
                lease_id=claimed.lease_id,
                expected_lease_version=claimed.lease_version,
                expected_state_version=claimed.run.state_version,
                signal=RunSignal.INTERRUPTED,
                pause_reason=None,
                limit_reached=False,
                checkpoint={"phase": "rolled_back"},
                checkpoint_replay_safe=True,
                checkpoint_effect_status=CheckpointEffectStatus.NONE,
                executed_ms=10,
                model_calls=0,
                tokens=0,
                request_id=f"rollback-{uuid.uuid4()}",
                capabilities=PLATFORM,
                appended=(
                    CanonicalMessage("assistant", (TextBlock(text="over limit"),)),
                ),
                workspace_cleanup_target=target,
                workspace_cleanup_sandbox_id=SANDBOX if target else None,
            )
        )


def _confirmation(claimed: Any, sandbox_id: uuid.UUID) -> ApplySignalCommand:
    return ApplySignalCommand(
        workspace_id=claimed.run.workspace_id,
        run_id=claimed.run.id,
        signal=RunSignal.LIMIT_CLEANUP_CONFIRMED,
        pause_reason=PauseReason.LIMIT,
        request_id=f"scheduler-{uuid.uuid4()}",
        capabilities=PLATFORM,
        confirmed_sandbox_id=sandbox_id,
    )


async def test_the_confirmation_requires_the_recorded_intent(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(sessions, claimed, target=None)

    async with sessions.begin() as db:
        with pytest.raises(InvalidStateMetadata):
            await SqlRunStore(db).apply_signal(_confirmation(claimed, SANDBOX))


async def test_the_confirmation_requires_the_recorded_sandbox(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(
        sessions, claimed, target=WorkspaceCleanupTarget.PAUSED_LIMIT
    )

    async with sessions.begin() as db:
        with pytest.raises(InvalidStateMetadata):
            await SqlRunStore(db).apply_signal(_confirmation(claimed, uuid.uuid4()))


async def test_the_confirmation_pauses_and_clears_the_intent_together(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(
        sessions, claimed, target=WorkspaceCleanupTarget.PAUSED_LIMIT
    )

    async with sessions.begin() as db:
        snapshot = await SqlRunStore(db).apply_signal(_confirmation(claimed, SANDBOX))

    assert snapshot.state is RunState.PAUSED
    assert snapshot.pause_reason is PauseReason.LIMIT
    async with sessions() as db:
        row = (
            await db.execute(select(RunRow).where(RunRow.id == claimed.run.id))
        ).scalar_one()
    assert row.workspace_cleanup_target is None
    assert row.workspace_cleanup_sandbox_id is None


async def test_a_wrong_target_is_refused_even_with_the_right_sandbox(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(
        sessions, claimed, target=WorkspaceCleanupTarget.FAILED_CONFLICT
    )

    async with sessions.begin() as db:
        with pytest.raises(InvalidStateMetadata):
            await SqlRunStore(db).apply_signal(_confirmation(claimed, SANDBOX))
