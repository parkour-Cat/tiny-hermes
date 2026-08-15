"""The checkpoint commit against real PostgreSQL: atomic, exclusive, honest.

Design §8 step 5 moves five facts in one transaction — revision row, Session
pointer, Run checkpoint marker, the slice's transcript turns, and the upload's
committed mark. These tests race two candidates, sabotage the transaction
between its parts, lose the answer on purpose, and check that the database
never shows a state between "happened" and "did not".
"""

import uuid
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.domain.models import (
    CheckpointEffectStatus,
    RunCapabilities,
    RunSignal,
)
from tiny_hermes.runs.infrastructure.sql_store import LeaseLost, SqlRunStore
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionMessageRow, SessionRow
from tiny_hermes.runs.ports.store import ClaimRunCommand, RecordSliceCommand
from tiny_hermes.session_workspace.application.committer import SqlWorkspaceLedger
from tiny_hermes.session_workspace.domain.models import CheckpointStatus, UploadKind, UploadStatus
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow
from tiny_hermes.session_workspace.ports.objects import (
    candidate_index_object,
    staging_prefix_key,
)
from tiny_hermes.session_workspace.ports.store import (
    CommitCheckpoint,
    RegisterUpload,
    UploadTotals,
)

PLATFORM = RunCapabilities(can_control=True, can_retry=True)


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def ledger(sessions: async_sessionmaker[AsyncSession]) -> SqlWorkspaceLedger:
    return SqlWorkspaceLedger(sessions)


@pytest.fixture
async def claimed(
    sessions: async_sessionmaker[AsyncSession],
    submitted_run: dict[str, Any],
) -> Any:
    del submitted_run
    async with sessions.begin() as db:
        claim = await SqlRunStore(db).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="committer-test",
                lease_seconds=60,
                request_id="claim-committer",
                capabilities=PLATFORM,
            )
        )
    assert claim is not None
    return claim


async def _ready_upload(
    ledger: SqlWorkspaceLedger,
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
) -> tuple[RegisterUpload, UUID]:
    """Walk a registration to `ready`, the state a commit departs from."""
    upload_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    workspace_id = claimed.run.workspace_id
    session_id = claimed.run.session_id
    registration = RegisterUpload(
        upload_id=upload_id,
        kind=UploadKind.WORKSPACE,
        workspace_id=workspace_id,
        session_id=session_id,
        run_id=claimed.run.id,
        base_revision_id=None,
        candidate_revision_id=revision_id,
        candidate_artifact_id=None,
        staging_prefix=staging_prefix_key(
            workspace_id=workspace_id, session_id=session_id, upload_id=upload_id
        ),
        candidate_index_key=candidate_index_object(
            workspace_id=workspace_id, session_id=session_id, upload_id=upload_id
        ).key,
        expires_at=_soon(),
    )
    await ledger.register_upload(registration)
    await ledger.mark_finalizing(upload_id, index_sha256="c" * 64)
    await ledger.mark_ready(
        upload_id, totals=UploadTotals(total_bytes=5, object_count=1)
    )
    del sessions
    return registration, revision_id


def _soon() -> Any:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    return datetime.now(UTC) + timedelta(hours=1)


def slice_command(claimed: Any, *, lease_id: Any | None = None) -> RecordSliceCommand:
    from tiny_hermes.runs.domain.models import CanonicalMessage, TextBlock  # noqa: PLC0415

    return RecordSliceCommand(
        workspace_id=claimed.run.workspace_id,
        run_id=claimed.run.id,
        lease_id=claimed.lease_id if lease_id is None else lease_id,
        expected_state_version=claimed.run.state_version,
        signal=RunSignal.COMPLETED,
        pause_reason=None,
        limit_reached=False,
        checkpoint={"phase": "post_tool"},
        checkpoint_replay_safe=True,
        checkpoint_effect_status=CheckpointEffectStatus.NONE,
        executed_ms=1_000,
        model_calls=1,
        tokens=10,
        request_id=f"slice-{uuid.uuid4()}",
        capabilities=PLATFORM,
        appended=(
            CanonicalMessage("assistant", (TextBlock(text="checkpointed"),)),
        ),
    )


def _commit(
    registration: RegisterUpload, revision_id: UUID, claimed: Any, **overrides: Any
) -> CommitCheckpoint:
    fields: dict[str, Any] = {
        "upload_id": registration.upload_id,
        "workspace_id": registration.workspace_id,
        "session_id": registration.session_id,
        "run_id": registration.run_id,
        "base_revision_id": registration.base_revision_id,
        "revision_id": revision_id,
        "manifest_object_key": f"manifests/{revision_id}.json",
        "manifest_sha256": "a" * 64,
        "manifest_schema_version": 1,
        "total_bytes": 5,
        "object_count": 1,
        "slice_command": slice_command(claimed),
    }
    fields.update(overrides)
    return CommitCheckpoint(**fields)


async def _pointer(
    sessions: async_sessionmaker[AsyncSession], session_id: UUID
) -> UUID | None:
    async with sessions() as db:
        return (
            await db.execute(
                select(SessionRow.workspace_revision_id).where(SessionRow.id == session_id)
            )
        ).scalar_one()


async def test_two_commits_from_one_base_revision_exactly_one_advances(
    ledger: SqlWorkspaceLedger,
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
) -> None:
    first, first_revision = await _ready_upload(ledger, sessions, claimed)
    second, second_revision = await _ready_upload(ledger, sessions, claimed)

    won = await ledger.commit(_commit(first, first_revision, claimed))
    lost = await ledger.commit(_commit(second, second_revision, claimed))

    assert won.status is CheckpointStatus.COMMITTED
    assert lost.status is CheckpointStatus.CONFLICT
    assert await _pointer(sessions, claimed.run.session_id) == first_revision
    async with sessions() as db:
        loser = await SqlWorkspaceStore(db).read(second.upload_id)
    assert loser is not None
    assert loser.status is UploadStatus.ABANDONED
    assert loser.abandon_reason == "workspace_conflict"


async def test_commit_transaction_is_atomic_across_all_five_tables(
    ledger: SqlWorkspaceLedger,
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
) -> None:
    """Sabotage the middle of the transaction and watch nothing move.

    A lease id that no longer owns the Run makes `record_slice` refuse
    *after* the revision row and pointer were staged in the same transaction.
    If any of the five tables changed, the transaction leaked.
    """
    registration, revision_id = await _ready_upload(ledger, sessions, claimed)
    poisoned = _commit(
        registration,
        revision_id,
        claimed,
        slice_command=slice_command(claimed, lease_id=uuid.uuid4()),
    )

    with pytest.raises(LeaseLost):
        await ledger.commit(poisoned)

    assert await _pointer(sessions, claimed.run.session_id) is None
    async with sessions() as db:
        revisions = (
            await db.execute(select(func.count()).select_from(WorkspaceRevisionRow))
        ).scalar_one()
        messages = (
            await db.execute(
                select(func.count())
                .select_from(SessionMessageRow)
                .where(SessionMessageRow.role == "assistant")
            )
        ).scalar_one()
        marker = (
            await db.execute(
                select(RunRow.checkpoint_workspace_revision_id).where(
                    RunRow.id == claimed.run.id
                )
            )
        ).scalar_one()
        upload = await SqlWorkspaceStore(db).read(registration.upload_id)
    assert revisions == 0
    assert messages == 0
    assert marker is None
    assert upload is not None and upload.status is UploadStatus.READY


async def test_lost_database_answer_reconciles_by_upload_id(
    ledger: SqlWorkspaceLedger,
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
) -> None:
    """A retry after an unknown outcome believes the row, not its memory."""
    registration, revision_id = await _ready_upload(ledger, sessions, claimed)
    command = _commit(registration, revision_id, claimed)

    first = await ledger.commit(command)
    assert first.status is CheckpointStatus.COMMITTED
    # The caller never saw that answer; it asks again with the same command.
    second = await ledger.commit(command)

    assert second.status is CheckpointStatus.COMMITTED
    async with sessions() as db:
        revisions = (
            await db.execute(select(func.count()).select_from(WorkspaceRevisionRow))
        ).scalar_one()
    assert revisions == 1, "reconciliation must not commit twice"
    assert await _pointer(sessions, claimed.run.session_id) == revision_id


async def test_conflict_marks_candidate_abandoned_and_keeps_the_pointer(
    ledger: SqlWorkspaceLedger,
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
) -> None:
    winner, winner_revision = await _ready_upload(ledger, sessions, claimed)
    outcome = await ledger.commit(_commit(winner, winner_revision, claimed))
    assert outcome.status is CheckpointStatus.COMMITTED

    # A stale candidate built on the now-superseded null base.
    stale, stale_revision = await _ready_upload(ledger, sessions, claimed)
    lost = await ledger.commit(_commit(stale, stale_revision, claimed))

    assert lost.status is CheckpointStatus.CONFLICT
    assert await _pointer(sessions, claimed.run.session_id) == winner_revision
    async with sessions() as db:
        row = await SqlWorkspaceStore(db).read(stale.upload_id)
    assert row is not None
    assert row.status is UploadStatus.ABANDONED
    assert row.cleanup_pending, "the collector still owes this candidate a sweep"
