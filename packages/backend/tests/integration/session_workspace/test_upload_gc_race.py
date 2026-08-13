"""The collector against real MinIO and PostgreSQL, with commits in flight.

Design §6.2's central promise: `finalizing` and `ready` rows and their durable
candidate indexes are GC roots, so a concurrent collector cannot delete an
object that a commit is still placing. These tests run a full reclaim pass over
everything the store lets the collector claim, and then check what survived.
"""

import os
import socket
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.session_workspace.application.cleanup import (
    CandidateIndex,
    Reference,
    encode_candidate_index,
    reclaim_upload,
)
from tiny_hermes.session_workspace.domain.models import UploadKind, UploadStatus
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.ports.objects import (
    ObjectRef,
    ObjectStorageUnavailable,
    blob_object,
    candidate_index_object,
    staging_object,
    staging_prefix_key,
)
from tiny_hermes.session_workspace.ports.store import RegisterUpload, UploadTotals

HOUR = timedelta(hours=1)

Ids = tuple[UUID, UUID, UUID]


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="module")
def objects() -> MinioObjectStore:
    return MinioObjectStore(
        endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "tiny-hermes-local"),
        secret_key=os.environ.get("S3_SECRET_KEY", "tiny-hermes-local-password"),
        bucket=os.environ.get("S3_BUCKET", "tiny-hermes-test"),
    )


@pytest.fixture(autouse=True)
async def reachable(objects: MinioObjectStore) -> None:
    parsed = urlparse(os.environ.get("S3_ENDPOINT", "http://localhost:9000"))
    try:
        socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 9000), timeout=1
        ).close()
    except OSError as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")
    try:
        await objects.ensure_bucket()
    except ObjectStorageUnavailable as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")


@pytest.fixture
async def scoped(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as value:
        yield value


@pytest.fixture
def ids(workspace_id: str, session_id: str, submitted_run: dict[str, Any]) -> Ids:
    return UUID(workspace_id), UUID(session_id), UUID(str(submitted_run["id"]))


class _Oracle:
    """A reference oracle scripted per key; everything else is unreferenced."""

    def __init__(self, verdicts: dict[str, Reference] | None = None) -> None:
        self._verdicts = verdicts or {}

    async def referenced(self, key: str) -> Reference:
        return self._verdicts.get(key, Reference.UNREFERENCED)


class _RefusingDeletes:
    """The real store, except deletion fails — a MinIO outage in one method."""

    def __init__(self, inner: MinioObjectStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def delete_many(self, refs: Sequence[ObjectRef]) -> None:
        raise ObjectStorageUnavailable("scripted outage")


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _registration(ids: Ids, *, expires_at: datetime) -> RegisterUpload:
    workspace, session, run = ids
    upload_id = uuid4()
    return RegisterUpload(
        upload_id=upload_id,
        kind=UploadKind.WORKSPACE,
        workspace_id=workspace,
        session_id=session,
        run_id=run,
        base_revision_id=None,
        candidate_revision_id=uuid4(),
        candidate_artifact_id=None,
        staging_prefix=staging_prefix_key(
            workspace_id=workspace, session_id=session, upload_id=upload_id
        ),
        candidate_index_key=candidate_index_object(
            workspace_id=workspace, session_id=session, upload_id=upload_id
        ).key,
        expires_at=expires_at,
    )


async def _stage_candidate(
    objects: MinioObjectStore,
    store: SqlWorkspaceStore,
    ids: Ids,
    *,
    expires_at: datetime,
    final_bodies: dict[str, bytes],
) -> tuple[RegisterUpload, tuple[ObjectRef, ...]]:
    """Register an upload and place its staging, final, and index objects."""
    workspace, session, _ = ids
    command = _registration(ids, expires_at=expires_at)
    await store.register_upload(command)

    staged = staging_object(
        workspace_id=workspace,
        session_id=session,
        upload_id=command.upload_id,
        name="body-0",
    )
    await objects.put_stream(staged, _one(b"staged bytes"), limit_bytes=100)

    finals: list[ObjectRef] = []
    for digest, body in final_bodies.items():
        ref = blob_object(workspace_id=workspace, session_id=session, digest=digest)
        await objects.put_stream(ref, _one(body), limit_bytes=1_000)
        finals.append(ref)

    index = CandidateIndex(
        upload_id=command.upload_id,
        final_keys=tuple(ref.key for ref in finals),
    )
    encoded = encode_candidate_index(index)
    stored = await objects.put_stream(
        ObjectRef(key=command.candidate_index_key), _one(encoded), limit_bytes=10_000
    )
    await store.mark_finalizing(command.upload_id, index_sha256=stored.sha256)
    return command, tuple(finals)


async def _collect_everything(
    store: SqlWorkspaceStore,
    objects: Any,
    *,
    oracle: _Oracle | None = None,
    now: datetime | None = None,
) -> list[Any]:
    outcomes = [
        await reclaim_upload(upload, store=store, objects=objects, oracle=oracle or _Oracle())
        for upload in await store.claim_cleanup(now or _now(), limit=50)
    ]
    return outcomes


async def test_a_live_candidates_final_objects_survive_a_full_gc_pass(
    scoped: AsyncSession, objects: MinioObjectStore, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    digest = "1" * 64
    command, finals = await _stage_candidate(
        objects, store, ids, expires_at=_now() + HOUR, final_bodies={digest: b"body"}
    )

    await _collect_everything(store, objects)
    assert await objects.stat(finals[0]) is not None

    # Still protected once ready — the commit transaction may be in flight.
    await store.mark_ready(
        command.upload_id, totals=UploadTotals(total_bytes=4, object_count=1)
    )
    await _collect_everything(store, objects)
    assert await objects.stat(finals[0]) is not None
    assert await objects.stat(ObjectRef(key=command.candidate_index_key)) is not None

    row = await store.read(command.upload_id)
    assert row is not None and row.status is UploadStatus.READY


async def test_an_expired_uploading_row_reclaims_staging_and_nothing_else(
    scoped: AsyncSession, objects: MinioObjectStore, ids: Ids
) -> None:
    workspace, session, _ = ids
    store = SqlWorkspaceStore(scoped)
    command = _registration(ids, expires_at=_now() - HOUR)
    await store.register_upload(command)
    staged = staging_object(
        workspace_id=workspace,
        session_id=session,
        upload_id=command.upload_id,
        name="orphan",
    )
    await objects.put_stream(staged, _one(b"orphaned"), limit_bytes=100)

    outcomes = await _collect_everything(store, objects)

    assert [outcome.finished for outcome in outcomes] == [True]
    assert await objects.stat(staged) is None
    row = await store.read(command.upload_id)
    assert row is not None
    assert row.status is UploadStatus.EXPIRED
    assert not row.cleanup_pending


async def test_abandoned_cleanup_follows_the_index_and_spares_referenced_blobs(
    scoped: AsyncSession, objects: MinioObjectStore, ids: Ids
) -> None:
    workspace, session, _ = ids
    store = SqlWorkspaceStore(scoped)
    kept_digest, doomed_digest = "2" * 64, "3" * 64
    command, _ = await _stage_candidate(
        objects,
        store,
        ids,
        expires_at=_now() + HOUR,
        final_bodies={kept_digest: b"kept", doomed_digest: b"doomed"},
    )
    await store.abandon(command.upload_id, reason="workspace_conflict")

    kept = blob_object(workspace_id=workspace, session_id=session, digest=kept_digest)
    oracle = _Oracle({kept.key: Reference.REFERENCED})
    outcomes = await _collect_everything(store, objects, oracle=oracle)

    assert [outcome.finished for outcome in outcomes] == [True]
    assert await objects.stat(kept) is not None, "another root still needs this blob"
    doomed = blob_object(
        workspace_id=workspace, session_id=session, digest=doomed_digest
    )
    assert await objects.stat(doomed) is None
    assert await objects.stat(ObjectRef(key=command.candidate_index_key)) is None
    row = await store.read(command.upload_id)
    assert row is not None and row.status is UploadStatus.EXPIRED


async def test_an_uncertain_reference_keeps_the_object_and_the_claim(
    scoped: AsyncSession, objects: MinioObjectStore, ids: Ids
) -> None:
    workspace, session, _ = ids
    store = SqlWorkspaceStore(scoped)
    digest = "4" * 64
    command, finals = await _stage_candidate(
        objects, store, ids, expires_at=_now() + HOUR, final_bodies={digest: b"maybe"}
    )
    await store.abandon(command.upload_id, reason="lease lost")

    blob = blob_object(workspace_id=workspace, session_id=session, digest=digest)
    oracle = _Oracle({blob.key: Reference.UNCERTAIN})
    outcomes = await _collect_everything(store, objects, oracle=oracle)

    assert [outcome.finished for outcome in outcomes] == [False]
    assert await objects.stat(finals[0]) is not None
    assert await objects.stat(ObjectRef(key=command.candidate_index_key)) is not None, (
        "the index is the only enumeration of final keys; it must outlive doubt"
    )
    row = await store.read(command.upload_id)
    assert row is not None
    assert row.status is UploadStatus.ABANDONED
    assert row.cleanup_pending, "the claim stays retryable"
    # And the next pass, with doubt resolved, actually finishes.
    finished = await _collect_everything(store, objects)
    assert [outcome.finished for outcome in finished] == [True]


async def test_a_committed_row_with_failed_deletion_keeps_its_debt(
    scoped: AsyncSession, objects: MinioObjectStore, ids: Ids
) -> None:
    store = SqlWorkspaceStore(scoped)
    digest = "5" * 64
    command, _ = await _stage_candidate(
        objects, store, ids, expires_at=_now() + HOUR, final_bodies={digest: b"live"}
    )
    await store.mark_ready(
        command.upload_id, totals=UploadTotals(total_bytes=4, object_count=1)
    )
    from tiny_hermes.session_workspace.infrastructure.tables import (  # noqa: PLC0415
        WorkspaceRevisionRow,
    )

    assert command.candidate_revision_id is not None
    scoped.add(
        WorkspaceRevisionRow(
            id=command.candidate_revision_id,
            workspace_id=ids[0],
            session_id=ids[1],
            parent_revision_id=None,
            manifest_schema_version=1,
            manifest_object_key=f"manifests/{command.candidate_revision_id}.json",
            manifest_sha256="0" * 64,
            total_bytes=4,
            object_count=1,
            created_by_run_id=ids[2],
        )
    )
    await scoped.flush()
    await store.mark_committed(
        command.upload_id,
        revision_id=command.candidate_revision_id,
        artifact_id=None,
    )

    broken = await _collect_everything(store, _RefusingDeletes(objects))
    assert [outcome.finished for outcome in broken] == [False]
    row = await store.read(command.upload_id)
    assert row is not None
    assert row.status is UploadStatus.COMMITTED
    assert row.cleanup_pending, "`committed` never means cleanup was forgotten"

    healed = await _collect_everything(store, objects)
    assert [outcome.finished for outcome in healed] == [True]
    settled = await store.read(command.upload_id)
    assert settled is not None
    assert not settled.cleanup_pending
    assert await objects.stat(ObjectRef(key=command.candidate_index_key)) is None


async def test_an_index_that_fails_verification_freezes_the_cleanup(
    scoped: AsyncSession, objects: MinioObjectStore, ids: Ids
) -> None:
    """A corrupt enumeration must not be believed — nothing is deleted."""
    store = SqlWorkspaceStore(scoped)
    digest = "6" * 64
    command, finals = await _stage_candidate(
        objects, store, ids, expires_at=_now() + HOUR, final_bodies={digest: b"real"}
    )
    # Overwrite the index with one that names a different upload.
    forged = encode_candidate_index(
        CandidateIndex(upload_id=uuid4(), final_keys=(finals[0].key,))
    )
    await objects.put_stream(
        ObjectRef(key=command.candidate_index_key), _one(forged), limit_bytes=10_000
    )
    await store.abandon(command.upload_id, reason="testing forgery")

    outcomes = await _collect_everything(store, objects)

    assert [outcome.finished for outcome in outcomes] == [False]
    assert await objects.stat(finals[0]) is not None
    row = await store.read(command.upload_id)
    assert row is not None and row.cleanup_pending
