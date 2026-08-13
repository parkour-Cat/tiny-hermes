"""The ObjectUpload state graph, enforced by PostgreSQL rather than promised.

Design §6.2 allows exactly these walks:

    uploading -> finalizing -> ready -> committed
    uploading/finalizing/ready -> abandoned -> expired
    uploading/finalizing/ready -> expired (TTL cleanup)

Every transition here carries its expected prior state into the UPDATE itself,
so the tests drive two sessions against the same row and let the database — not
test scheduling — decide who wins.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.session_workspace.domain.models import UploadKind, UploadStatus
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow
from tiny_hermes.session_workspace.ports.objects import (
    candidate_index_object,
    staging_prefix_key,
)
from tiny_hermes.session_workspace.ports.store import (
    RegisterUpload,
    UnknownUpload,
    UploadStateConflict,
    UploadTotals,
)

HOUR = timedelta(hours=1)
INDEX_SHA = "b" * 64
TOTALS = UploadTotals(total_bytes=123, object_count=4)

Ids = tuple[UUID, UUID, UUID]


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def scoped(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessions() as value:
        yield value


@pytest.fixture
def ids(workspace_id: str, session_id: str, submitted_run: dict[str, Any]) -> Ids:
    return UUID(workspace_id), UUID(session_id), UUID(str(submitted_run["id"]))


def _registration(
    ids: Ids,
    *,
    kind: UploadKind = UploadKind.WORKSPACE,
    expires_at: datetime | None = None,
    staging_prefix: str | None = None,
) -> RegisterUpload:
    workspace, session, run = ids
    upload_id = uuid4()
    return RegisterUpload(
        upload_id=upload_id,
        kind=kind,
        workspace_id=workspace,
        session_id=session,
        run_id=run,
        base_revision_id=None,
        candidate_revision_id=uuid4() if kind is UploadKind.WORKSPACE else None,
        candidate_artifact_id=uuid4() if kind is UploadKind.ARTIFACT else None,
        staging_prefix=staging_prefix
        or staging_prefix_key(
            workspace_id=workspace, session_id=session, upload_id=upload_id
        ),
        candidate_index_key=candidate_index_object(
            workspace_id=workspace, session_id=session, upload_id=upload_id
        ).key,
        expires_at=expires_at or _now() + HOUR,
    )


async def _insert_revision(session: AsyncSession, ids: Ids, revision_id: UUID) -> None:
    """The immutable row a committed workspace upload must point at."""
    workspace, workspace_session, run = ids
    session.add(
        WorkspaceRevisionRow(
            id=revision_id,
            workspace_id=workspace,
            session_id=workspace_session,
            parent_revision_id=None,
            manifest_schema_version=1,
            manifest_object_key=f"manifests/{revision_id}.json",
            manifest_sha256="0" * 64,
            total_bytes=0,
            object_count=0,
            created_by_run_id=run,
        )
    )
    await session.flush()


async def test_the_commit_walk_records_every_station(
    scoped: AsyncSession, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    command = _registration(ids)

    registered = await store.register_upload(command)
    assert registered.status is UploadStatus.UPLOADING
    assert registered.cleanup_pending, "staging exists from birth, so does the debt"
    assert registered.staging_prefix == command.staging_prefix
    assert registered.candidate_index_key == command.candidate_index_key

    finalizing = await store.mark_finalizing(command.upload_id, index_sha256=INDEX_SHA)
    assert finalizing.status is UploadStatus.FINALIZING
    assert finalizing.candidate_index_sha256 == INDEX_SHA

    ready = await store.mark_ready(command.upload_id, totals=TOTALS)
    assert ready.status is UploadStatus.READY
    assert ready.total_bytes == TOTALS.total_bytes
    assert ready.object_count == TOTALS.object_count

    assert command.candidate_revision_id is not None
    await _insert_revision(scoped, ids, command.candidate_revision_id)
    await store.mark_committed(
        command.upload_id,
        revision_id=command.candidate_revision_id,
        artifact_id=None,
    )
    committed = await store.read(command.upload_id)
    assert committed is not None
    assert committed.status is UploadStatus.COMMITTED
    assert committed.committed_revision_id == command.candidate_revision_id
    assert committed.cleanup_pending, "committed never means cleanup happened"


async def test_every_transition_names_its_expected_prior_state(
    scoped: AsyncSession, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    command = _registration(ids)
    await store.register_upload(command)

    # Not yet finalizing, so ready is out of order.
    with pytest.raises(UploadStateConflict):
        await store.mark_ready(command.upload_id, totals=TOTALS)
    # And an uploading candidate has no verified index to commit.
    with pytest.raises(UploadStateConflict):
        await store.mark_committed(
            command.upload_id,
            revision_id=command.candidate_revision_id,
            artifact_id=None,
        )

    await store.mark_finalizing(command.upload_id, index_sha256=INDEX_SHA)
    with pytest.raises(UploadStateConflict):
        await store.mark_finalizing(command.upload_id, index_sha256=INDEX_SHA)

    await store.mark_ready(command.upload_id, totals=TOTALS)
    await store.abandon(command.upload_id, reason="run cancelled")
    abandoned = await store.read(command.upload_id)
    assert abandoned is not None
    assert abandoned.status is UploadStatus.ABANDONED
    assert abandoned.abandon_reason == "run cancelled"

    # Terminal states refuse further movement.
    with pytest.raises(UploadStateConflict):
        await store.abandon(command.upload_id, reason="twice")
    with pytest.raises(UploadStateConflict):
        await store.mark_ready(command.upload_id, totals=TOTALS)


async def test_an_unknown_upload_is_reported_as_unknown(
    scoped: AsyncSession, ids: Ids
) -> None:
    del ids
    store = SqlWorkspaceStore(scoped)
    with pytest.raises(UnknownUpload):
        await store.mark_finalizing(uuid4(), index_sha256=INDEX_SHA)
    assert await store.read(uuid4()) is None


async def test_commit_refuses_a_revision_that_was_never_registered(
    scoped: AsyncSession, ids: Ids
) -> None:
    """The commit confirms the registered candidate, not whatever was passed."""
    store = SqlWorkspaceStore(scoped)
    command = _registration(ids)
    await store.register_upload(command)
    await store.mark_finalizing(command.upload_id, index_sha256=INDEX_SHA)
    await store.mark_ready(command.upload_id, totals=TOTALS)

    imposter = uuid4()
    await _insert_revision(scoped, ids, imposter)
    with pytest.raises(UploadStateConflict):
        await store.mark_committed(
            command.upload_id, revision_id=imposter, artifact_id=None
        )
    unchanged = await store.read(command.upload_id)
    assert unchanged is not None and unchanged.status is UploadStatus.READY


async def test_the_staging_prefix_is_taken_exactly_once(
    scoped: AsyncSession, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    command = _registration(ids)
    await store.register_upload(command)
    duplicate = _registration(ids, staging_prefix=command.staging_prefix)
    with pytest.raises(IntegrityError):
        await store.register_upload(duplicate)


async def test_ttl_cleanup_claims_only_what_has_actually_expired(
    scoped: AsyncSession, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    stale = _registration(ids, expires_at=_now() - HOUR)
    fresh = _registration(ids)
    await store.register_upload(stale)
    await store.register_upload(fresh)
    await store.mark_finalizing(fresh.upload_id, index_sha256=INDEX_SHA)

    claimed = await store.claim_cleanup(_now(), limit=10)

    claimed_ids = {upload.upload_id for upload in claimed}
    assert stale.upload_id in claimed_ids
    assert fresh.upload_id not in claimed_ids, (
        "an unexpired finalizing candidate is a GC root, not a cleanup job"
    )


async def test_finish_cleanup_expires_the_reclaimed_and_settles_the_committed(
    scoped: AsyncSession, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    reclaimed = _registration(ids, expires_at=_now() - HOUR)
    await store.register_upload(reclaimed)
    await store.finish_cleanup(reclaimed.upload_id)
    expired = await store.read(reclaimed.upload_id)
    assert expired is not None
    assert expired.status is UploadStatus.EXPIRED
    assert not expired.cleanup_pending

    committed = _registration(ids)
    await store.register_upload(committed)
    await store.mark_finalizing(committed.upload_id, index_sha256=INDEX_SHA)
    await store.mark_ready(committed.upload_id, totals=TOTALS)
    assert committed.candidate_revision_id is not None
    await _insert_revision(scoped, ids, committed.candidate_revision_id)
    await store.mark_committed(
        committed.upload_id,
        revision_id=committed.candidate_revision_id,
        artifact_id=None,
    )
    await store.finish_cleanup(committed.upload_id)
    settled = await store.read(committed.upload_id)
    assert settled is not None
    assert settled.status is UploadStatus.COMMITTED, "history is kept, not rewritten"
    assert not settled.cleanup_pending

    # Finishing twice is a state error, not a silent no-op: the second caller's
    # claim was stale.
    with pytest.raises(UploadStateConflict):
        await store.finish_cleanup(reclaimed.upload_id)


async def test_an_unknown_database_result_is_resolved_by_rereading(
    sessions: async_sessionmaker[AsyncSession], ids: Ids
) -> None:
    """Design §5.4: after an unknown outcome, the row is the truth.

    A connection failure alone never changes the row to `abandoned` — the
    rolled-back transaction leaves `ready` exactly as it was, and only the
    re-read may decide what happened.
    """
    async with sessions() as setup:
        store = SqlWorkspaceStore(setup)
        command = _registration(ids)
        await store.register_upload(command)
        await store.mark_finalizing(command.upload_id, index_sha256=INDEX_SHA)
        await store.mark_ready(command.upload_id, totals=TOTALS)
        assert command.candidate_revision_id is not None
        await _insert_revision(setup, ids, command.candidate_revision_id)
        await setup.commit()

    # The commit attempt whose outcome the caller never learns.
    async with sessions() as doomed:
        await SqlWorkspaceStore(doomed).mark_committed(
            command.upload_id,
            revision_id=command.candidate_revision_id,
            artifact_id=None,
        )
        await doomed.rollback()

    async with sessions() as reread:
        truth = await SqlWorkspaceStore(reread).read(command.upload_id)
    assert truth is not None
    assert truth.status is UploadStatus.READY, (
        "a failed connection is not a conflict; the row must not move"
    )
