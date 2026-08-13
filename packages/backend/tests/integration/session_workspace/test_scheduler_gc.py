"""The Scheduler's workspace garbage collection, against real stores.

Design §13: expired staging is reclaimed in candidate-index order, GC roots
protect commits in flight, blob references are calculated from retained
manifests rather than guessed, artifact retention is enforced, and a Run
whose rollback recorded a destination reaches it only when the destruction is
confirmed.
"""

import hashlib
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.artifacts.infrastructure.sql_store import SqlArtifactStore
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.tables import SessionRow
from tiny_hermes.session_workspace.application.cleanup import (
    CandidateIndex,
    encode_candidate_index,
)
from tiny_hermes.session_workspace.domain.models import UploadKind, UploadStatus
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.infrastructure.tables import (
    ObjectUploadRow,
    WorkspaceRevisionRow,
)
from tiny_hermes.session_workspace.ports.objects import (
    ObjectRef,
    blob_object,
    candidate_index_object,
    manifest_object,
    staging_object,
    staging_prefix_key,
)
from tiny_hermes.session_workspace.ports.store import RegisterUpload

HOUR = timedelta(hours=1)


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
    await objects.ensure_bucket()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def scheduler(
    sessions: async_sessionmaker[AsyncSession], objects: MinioObjectStore
) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=sessions,
        notifier=NullWakeUpNotifier(),
        objects=objects,
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=24),
    )


@pytest.fixture
def ids(
    workspace_id: str, session_id: str, submitted_run: dict[str, Any]
) -> tuple[UUID, UUID, UUID]:
    return UUID(workspace_id), UUID(session_id), UUID(str(submitted_run["id"]))


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _registered_upload(
    sessions: async_sessionmaker[AsyncSession],
    ids: tuple[UUID, UUID, UUID],
    *,
    expires_at: datetime,
) -> RegisterUpload:
    workspace, session, run = ids
    upload_id = uuid.uuid4()
    registration = RegisterUpload(
        upload_id=upload_id,
        kind=UploadKind.WORKSPACE,
        workspace_id=workspace,
        session_id=session,
        run_id=run,
        base_revision_id=None,
        candidate_revision_id=uuid.uuid4(),
        candidate_artifact_id=None,
        staging_prefix=staging_prefix_key(
            workspace_id=workspace, session_id=session, upload_id=upload_id
        ),
        candidate_index_key=candidate_index_object(
            workspace_id=workspace, session_id=session, upload_id=upload_id
        ).key,
        expires_at=expires_at,
    )
    async with sessions.begin() as db:
        await SqlWorkspaceStore(db).register_upload(registration)
    return registration


async def _status_of(
    sessions: async_sessionmaker[AsyncSession], upload_id: UUID
) -> tuple[UploadStatus, bool]:
    async with sessions() as db:
        row = await SqlWorkspaceStore(db).read(upload_id)
    assert row is not None
    return row.status, row.cleanup_pending


async def test_staging_uploads_expire_after_ttl_in_candidate_index_order(
    scheduler: SchedulerRuntime,
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    ids: tuple[UUID, UUID, UUID],
) -> None:
    workspace, session, _ = ids
    stale = await _registered_upload(sessions, ids, expires_at=datetime.now(UTC) - HOUR)
    staged = staging_object(
        workspace_id=workspace,
        session_id=session,
        upload_id=stale.upload_id,
        name="body",
    )
    await objects.put_stream(staged, _one(b"orphan"), limit_bytes=100)

    await scheduler.run_once()

    assert await objects.stat(staged) is None
    status, pending = await _status_of(sessions, stale.upload_id)
    assert status is UploadStatus.EXPIRED
    assert not pending


async def test_gc_roots_protect_finalizing_and_ready_candidates(
    scheduler: SchedulerRuntime,
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    ids: tuple[UUID, UUID, UUID],
) -> None:
    workspace, session, _ = ids
    live = await _registered_upload(sessions, ids, expires_at=datetime.now(UTC) + HOUR)
    digest = "7" * 64
    final = blob_object(workspace_id=workspace, session_id=session, digest=digest)
    await objects.put_stream(final, _one(b"committing"), limit_bytes=100)
    index = encode_candidate_index(
        CandidateIndex(upload_id=live.upload_id, final_keys=(final.key,))
    )
    stored = await objects.put_stream(
        ObjectRef(key=live.candidate_index_key), _one(index), limit_bytes=10_000
    )
    async with sessions.begin() as db:
        await SqlWorkspaceStore(db).mark_finalizing(
            live.upload_id, index_sha256=stored.sha256
        )

    await scheduler.run_once()

    assert await objects.stat(final) is not None, "a commit in flight is a GC root"
    status, _ = await _status_of(sessions, live.upload_id)
    assert status is UploadStatus.FINALIZING


async def test_blob_refcount_is_calculated_from_retained_manifests(
    scheduler: SchedulerRuntime,
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    ids: tuple[UUID, UUID, UUID],
) -> None:
    """An abandoned candidate's blob survives when a retained revision uses it."""
    workspace, session, run = ids
    kept_digest = hashlib.sha256(b"kept body").hexdigest()
    doomed_digest = hashlib.sha256(b"doomed body").hexdigest()
    kept = blob_object(workspace_id=workspace, session_id=session, digest=kept_digest)
    doomed = blob_object(workspace_id=workspace, session_id=session, digest=doomed_digest)
    await objects.put_stream(kept, _one(b"kept body"), limit_bytes=100)
    await objects.put_stream(doomed, _one(b"doomed body"), limit_bytes=100)

    # A retained revision whose manifest names only the kept digest.
    revision_id = uuid.uuid4()
    manifest = json.dumps(
        {
            "schema_version": 1,
            "entries": [
                {
                    "path": "kept.txt",
                    "type": "file",
                    "mode": 0o644,
                    "size": 9,
                    "sha256": kept_digest,
                }
            ],
            "total_bytes": 9,
            "object_count": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_ref = manifest_object(
        workspace_id=workspace, session_id=session, revision_id=revision_id
    )
    await objects.put_stream(manifest_ref, _one(manifest), limit_bytes=10_000)
    async with sessions.begin() as db:
        db.add(
            WorkspaceRevisionRow(
                id=revision_id,
                workspace_id=workspace,
                session_id=session,
                parent_revision_id=None,
                manifest_schema_version=1,
                manifest_object_key=manifest_ref.key,
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                total_bytes=9,
                object_count=1,
                created_by_run_id=run,
            )
        )
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id == session)
            .values(workspace_revision_id=revision_id)
        )

    # The abandoned candidate names both blobs in its durable index.
    candidate = await _registered_upload(
        sessions, ids, expires_at=datetime.now(UTC) + HOUR
    )
    index = encode_candidate_index(
        CandidateIndex(
            upload_id=candidate.upload_id, final_keys=(kept.key, doomed.key)
        )
    )
    stored = await objects.put_stream(
        ObjectRef(key=candidate.candidate_index_key), _one(index), limit_bytes=10_000
    )
    async with sessions.begin() as db:
        store = SqlWorkspaceStore(db)
        await store.mark_finalizing(candidate.upload_id, index_sha256=stored.sha256)
        await store.abandon(candidate.upload_id, reason="workspace_conflict")

    await scheduler.run_once()

    assert await objects.stat(kept) is not None, "a retained manifest references it"
    assert await objects.stat(doomed) is None
    status, pending = await _status_of(sessions, candidate.upload_id)
    assert status is UploadStatus.EXPIRED
    assert not pending


async def test_committed_rows_with_cleanup_pending_are_retried(
    scheduler: SchedulerRuntime,
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    ids: tuple[UUID, UUID, UUID],
) -> None:
    """A commit whose staging deletion failed is somebody's debt, and it is paid."""
    workspace, session, _ = ids
    committed = await _registered_upload(
        sessions, ids, expires_at=datetime.now(UTC) + HOUR
    )
    staged = staging_object(
        workspace_id=workspace,
        session_id=session,
        upload_id=committed.upload_id,
        name="leftover",
    )
    await objects.put_stream(staged, _one(b"leftover"), limit_bytes=100)
    async with sessions.begin() as db:
        # The lifecycle walked, then the row forced into the shape a crashed
        # post-commit cleanup leaves: committed, debt unpaid.
        await db.execute(
            update(ObjectUploadRow)
            .where(ObjectUploadRow.id == committed.upload_id)
            .values(status=UploadStatus.COMMITTED.value, cleanup_pending=True)
        )

    await scheduler.run_once()

    assert await objects.stat(staged) is None
    status, pending = await _status_of(sessions, committed.upload_id)
    assert status is UploadStatus.COMMITTED, "history is kept, not rewritten"
    assert not pending


async def test_artifact_retention_is_enforced(
    scheduler: SchedulerRuntime,
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    ids: tuple[UUID, UUID, UUID],
) -> None:
    workspace, session, run = ids
    ref = ObjectRef(key=f"workspaces/{workspace}/runs/{run}/artifacts/{uuid.uuid4()}")
    await objects.put_stream(ref, _one(b"old output"), limit_bytes=100)
    stale = Artifact(
        id=uuid.uuid4(),
        workspace_id=workspace,
        session_id=session,
        run_id=run,
        object_key=ref.key,
        filename="old.log",
        media_type="text/plain",
        size_bytes=10,
        sha256=hashlib.sha256(b"old output").hexdigest(),
        truncated=False,
        expires_at=datetime.now(UTC) - HOUR,
    )
    async with sessions() as db:
        await SqlArtifactStore(db).insert(stale)
        await db.commit()

    await scheduler.run_once()

    assert await objects.stat(ref) is None
    async with sessions() as db:
        remaining = await SqlArtifactStore(db).read_scoped(stale.id, workspace)
    assert remaining is None


async def test_confirmed_cleanup_moves_an_intent_bearing_run_to_paused_limit(
    scheduler_with_sandbox: "tuple[SchedulerRuntime, Any]",
    sessions: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    ids: tuple[UUID, UUID, UUID],
) -> None:
    from tiny_hermes.runs.domain.models import WorkspaceCleanupTarget  # noqa: PLC0415
    from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore  # noqa: PLC0415
    from tiny_hermes.runs.ports.store import ClaimRunCommand  # noqa: PLC0415
    from tiny_hermes.sandbox.domain.models import (  # noqa: PLC0415
        InstanceStatus,
        SandboxInstance,
    )
    from tiny_hermes.sandbox.infrastructure.sql_store import (  # noqa: PLC0415
        SqlSandboxStore,
    )

    from .test_checkpoint_commit import PLATFORM, slice_command  # noqa: PLC0415

    runtime, cleanup_calls = scheduler_with_sandbox
    workspace, _, run = ids
    sandbox_id = uuid.uuid4()

    async with sessions.begin() as db:
        claim = await SqlRunStore(db).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="gc-drill",
                lease_seconds=1,
                request_id="claim-gc-drill",
                capabilities=PLATFORM,
            )
        )
        assert claim is not None
        await SqlSandboxStore(db).reserve(
            run_id=run,
            workspace_id=workspace,
            instance=SandboxInstance(
                id=sandbox_id,
                container_id="gone-already",
                image_digest="sha256:" + "e" * 64,
                resource_profile="default",
                boot_id="b1",
                status=InstanceStatus.ISOLATED,
            ),
        )

    # The rollback's own transaction: interruption, intent, and results.
    import dataclasses  # noqa: PLC0415

    from tiny_hermes.runs.domain.models import RunSignal  # noqa: PLC0415

    async with sessions.begin() as db:
        command = dataclasses.replace(
            slice_command(claim),
            signal=RunSignal.INTERRUPTED,
            workspace_cleanup_target=WorkspaceCleanupTarget.PAUSED_LIMIT,
            workspace_cleanup_sandbox_id=sandbox_id,
        )
        await SqlRunStore(db).record_slice(command)
    async with sessions.begin() as db:
        held = await SqlSandboxStore(db).live_for_run(run)
        assert held is not None
        await SqlSandboxStore(db).isolate(held.id, reason="import drill")

    await runtime.run_once()

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT status, pause_reason, workspace_cleanup_target "
                    "FROM runs WHERE id = :run"
                ),
                {"run": str(run)},
            )
        ).one()
    assert cleanup_calls, "the isolated reservation was handed to cleanup"
    assert row.status == "paused"
    assert row.pause_reason == "limit"
    assert row.workspace_cleanup_target is None


@pytest.fixture
def scheduler_with_sandbox(
    sessions: async_sessionmaker[AsyncSession], objects: MinioObjectStore
) -> "tuple[SchedulerRuntime, list[UUID]]":
    calls: list[UUID] = []

    class ConfirmingCleanup:
        async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
            del sandbox_id
            calls.append(run_id)

    runtime = SchedulerRuntime(
        session_factory=sessions,
        notifier=NullWakeUpNotifier(),
        sandbox=ConfirmingCleanup(),
        objects=objects,
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=24),
    )
    return runtime, calls