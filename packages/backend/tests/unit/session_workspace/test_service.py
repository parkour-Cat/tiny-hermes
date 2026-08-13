"""The two-operation workspace service, driven entirely through fake ports.

Design §5.1: callers see `restore` and `checkpoint`; manifest arithmetic,
object staging, upload lifecycle, and sandbox streams hide behind them. These
tests script the ports and assert the order and the refusals — the flows'
rules, not the adapters' mechanics.
"""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID

import pytest
from tiny_hermes.session_workspace.application.service import (
    SessionWorkspaceService,
    WorkspaceCheckpoint,
    WorkspaceIntegrityFailed,
    WorkspaceRestore,
)
from tiny_hermes.session_workspace.domain.models import (
    CheckpointStatus,
    UnsupportedWorkspaceEntry,
    WorkspaceQuota,
)
from tiny_hermes.session_workspace.ports.objects import (
    ObjectMissing,
    ObjectRef,
    ObjectStat,
    ObjectTooLarge,
    StoredObject,
    blob_object,
    manifest_object,
)
from tiny_hermes.session_workspace.ports.sandbox import ExportedFile, ScanEntry
from tiny_hermes.session_workspace.ports.store import (
    CommitOutcome,
    RegisterUpload,
    RevisionRecord,
    UploadTotals,
)

WORKSPACE = uuid.uuid4()
SESSION = uuid.uuid4()
RUN = uuid.uuid4()

QUOTA = WorkspaceQuota(max_bytes=1_000_000, max_objects=1_000)

BODY = b"hello workspace"
DIGEST = hashlib.sha256(BODY).hexdigest()


def _manifest_document(entries: list[dict[str, Any]]) -> bytes:
    files = [entry for entry in entries if entry["type"] == "file"]
    return json.dumps(
        {
            "schema_version": 1,
            "entries": entries,
            "total_bytes": sum(entry["size"] for entry in files),
            "object_count": len(entries),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


MANIFEST_BYTES = _manifest_document(
    [
        {"path": "notes", "type": "directory", "mode": 0o755, "size": 0, "sha256": None},
        {
            "path": "notes/a.txt",
            "type": "file",
            "mode": 0o644,
            "size": len(BODY),
            "sha256": DIGEST,
        },
    ]
)


class FakeObjects:
    """An in-memory ObjectStore that records the order of every operation."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    async def put_stream(
        self, ref: ObjectRef, chunks: AsyncIterator[bytes], *, limit_bytes: int
    ) -> StoredObject:
        received = b""
        async for chunk in chunks:
            received += chunk
            if len(received) > limit_bytes:
                raise ObjectTooLarge(ref.key)
        self.blobs[ref.key] = received
        self.calls.append(("put", ref.key))
        return StoredObject(size=len(received), sha256=hashlib.sha256(received).hexdigest())

    async def get_stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        self.calls.append(("get", ref.key))
        if ref.key not in self.blobs:
            raise ObjectMissing(ref.key)
        yield self.blobs[ref.key]

    async def stat(self, ref: ObjectRef) -> ObjectStat | None:
        self.calls.append(("stat", ref.key))
        body = self.blobs.get(ref.key)
        return None if body is None else ObjectStat(size=len(body))

    async def server_copy(self, source: ObjectRef, target: ObjectRef) -> None:
        self.calls.append(("copy", target.key))
        self.blobs[target.key] = self.blobs[source.key]

    async def delete_many(self, refs: Sequence[ObjectRef]) -> None:
        for ref in refs:
            self.calls.append(("delete", ref.key))
            self.blobs.pop(ref.key, None)

    async def list_prefix(self, prefix: str, *, limit: int) -> tuple[ObjectRef, ...]:
        found = [ObjectRef(key=key) for key in sorted(self.blobs) if key.startswith(prefix)]
        return tuple(found[:limit])


class FakeSandbox:
    def __init__(self) -> None:
        self.scan_entries: tuple[ScanEntry, ...] = ()
        self.imported = b""
        self.import_calls = 0
        self.files: dict[str, bytes] = {}

    async def scan(self) -> tuple[ScanEntry, ...]:
        return self.scan_entries

    async def import_tree(
        self, tar_stream: AsyncIterator[bytes], *, declared_total: int
    ) -> None:
        del declared_total
        self.import_calls += 1
        async for chunk in tar_stream:
            self.imported += chunk

    async def export_files(self, paths: Sequence[str]) -> AsyncIterator[ExportedFile]:
        for path in paths:
            body = self.files[path]
            yield ExportedFile(path=path, size=len(body), chunks=_one(body))


class FakeLedger:
    """Scripted persistence: records lifecycle calls, answers what it is told."""

    def __init__(self) -> None:
        self.revision: RevisionRecord | None = None
        self.calls: list[str] = []
        self.registered: RegisterUpload | None = None
        self.outcome = CommitOutcome(status=CheckpointStatus.COMMITTED, run=None)

    async def current_revision(
        self, workspace_id: UUID, session_id: UUID
    ) -> RevisionRecord | None:
        del workspace_id, session_id
        return self.revision

    async def register_upload(self, command: RegisterUpload) -> None:
        self.calls.append("register")
        self.registered = command

    async def mark_finalizing(self, upload_id: UUID, *, index_sha256: str) -> None:
        del upload_id, index_sha256
        self.calls.append("finalizing")

    async def mark_ready(self, upload_id: UUID, *, totals: UploadTotals) -> None:
        del upload_id, totals
        self.calls.append("ready")

    async def abandon(self, upload_id: UUID, *, reason: str) -> None:
        del upload_id
        self.calls.append(f"abandon:{reason}")

    async def commit(self, commit: Any) -> CommitOutcome:
        del commit
        self.calls.append("commit")
        return self.outcome

    async def settle(self, upload_id: UUID) -> None:
        del upload_id
        self.calls.append("settle")


def _scan_of_manifest() -> tuple[ScanEntry, ...]:
    return (
        ScanEntry(path="notes", entry_type="directory", mode=0o755, size=0, sha256=None),
        ScanEntry(
            path="notes/a.txt",
            entry_type="file",
            mode=0o644,
            size=len(BODY),
            sha256=DIGEST,
        ),
    )


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.fixture
def objects() -> FakeObjects:
    return FakeObjects()


@pytest.fixture
def sandbox() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def ledger() -> FakeLedger:
    return FakeLedger()


@pytest.fixture
def service(
    objects: FakeObjects, sandbox: FakeSandbox, ledger: FakeLedger
) -> SessionWorkspaceService:
    return SessionWorkspaceService(
        ledger=ledger,
        objects=objects,
        sandbox=sandbox,
        staging_ttl_seconds=3_600,
    )


def _revision_record(manifest_bytes: bytes) -> RevisionRecord:
    return RevisionRecord(
        revision_id=uuid.uuid4(),
        manifest_object_key=manifest_object(
            workspace_id=WORKSPACE, session_id=SESSION, revision_id=uuid.uuid4()
        ).key,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_schema_version=1,
        total_bytes=len(BODY),
        object_count=2,
    )


def _restore() -> WorkspaceRestore:
    return WorkspaceRestore(workspace_id=WORKSPACE, session_id=SESSION, run_id=RUN)


def _checkpoint(base: UUID | None = None) -> WorkspaceCheckpoint:
    return WorkspaceCheckpoint(
        workspace_id=WORKSPACE,
        session_id=SESSION,
        run_id=RUN,
        base_revision_id=base,
        quota=QUOTA,
        slice_command=object(),  # opaque to the service; the ledger receives it
    )


async def test_restore_of_null_revision_makes_no_object_call(
    service: SessionWorkspaceService, objects: FakeObjects, sandbox: FakeSandbox
) -> None:
    result = await service.restore(_restore())

    assert result.revision_id is None
    assert result.object_count == 0
    assert objects.calls == []
    assert sandbox.import_calls == 0


async def test_restore_verifies_manifest_before_any_body_and_refuses_mismatch(
    service: SessionWorkspaceService,
    objects: FakeObjects,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    record = _revision_record(MANIFEST_BYTES)
    # The stored manifest differs from what the revision row promised.
    objects.blobs[record.manifest_object_key] = MANIFEST_BYTES + b" "
    objects.blobs[
        blob_object(workspace_id=WORKSPACE, session_id=SESSION, digest=DIGEST).key
    ] = BODY
    ledger.revision = record

    with pytest.raises(WorkspaceIntegrityFailed):
        await service.restore(_restore())

    assert sandbox.import_calls == 0, "no body may move on a lying manifest"
    assert all(kind != "get" or key == record.manifest_object_key for kind, key in objects.calls)


async def test_restore_streams_bodies_and_verifies_the_rescan(
    service: SessionWorkspaceService,
    objects: FakeObjects,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    record = _revision_record(MANIFEST_BYTES)
    objects.blobs[record.manifest_object_key] = MANIFEST_BYTES
    objects.blobs[
        blob_object(workspace_id=WORKSPACE, session_id=SESSION, digest=DIGEST).key
    ] = BODY
    ledger.revision = record
    sandbox.scan_entries = _scan_of_manifest()

    result = await service.restore(_restore())

    assert result.revision_id == record.revision_id
    assert result.total_bytes == len(BODY)
    assert sandbox.import_calls == 1
    assert BODY in sandbox.imported, "the file body must be inside the tar"


async def test_restore_refuses_a_tree_that_does_not_match_after_import(
    service: SessionWorkspaceService,
    objects: FakeObjects,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    record = _revision_record(MANIFEST_BYTES)
    objects.blobs[record.manifest_object_key] = MANIFEST_BYTES
    objects.blobs[
        blob_object(workspace_id=WORKSPACE, session_id=SESSION, digest=DIGEST).key
    ] = BODY
    ledger.revision = record
    sandbox.scan_entries = (
        ScanEntry(path="notes", entry_type="directory", mode=0o755, size=0, sha256=None),
    )

    with pytest.raises(WorkspaceIntegrityFailed):
        await service.restore(_restore())


async def test_checkpoint_unchanged_creates_no_upload_and_no_revision(
    service: SessionWorkspaceService,
    objects: FakeObjects,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    record = _revision_record(MANIFEST_BYTES)
    objects.blobs[record.manifest_object_key] = MANIFEST_BYTES
    ledger.revision = record
    sandbox.scan_entries = _scan_of_manifest()

    result = await service.checkpoint(_checkpoint(base=record.revision_id))

    assert result.status is CheckpointStatus.UNCHANGED
    assert result.revision_id == record.revision_id
    assert ledger.calls == [], "no upload and no commit for an unchanged tree"


async def test_checkpoint_over_quota_refuses_before_uploading_bodies(
    service: SessionWorkspaceService,
    objects: FakeObjects,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    sandbox.scan_entries = (
        ScanEntry(
            path="huge.bin",
            entry_type="file",
            mode=0o644,
            size=QUOTA.max_bytes + 1,
            sha256="9" * 64,
        ),
    )

    result = await service.checkpoint(_checkpoint())

    assert result.status is CheckpointStatus.LIMIT_EXCEEDED
    assert result.measurement is not None
    assert result.measurement.dimension == "bytes"
    assert ledger.calls == []
    assert not [call for call in objects.calls if call[0] == "put"]


async def test_unsupported_entry_type_refuses_the_checkpoint(
    service: SessionWorkspaceService, sandbox: FakeSandbox, ledger: FakeLedger
) -> None:
    sandbox.scan_entries = (
        ScanEntry(path="sneaky", entry_type="symlink", mode=0o777, size=0, sha256=None),
    )

    with pytest.raises(UnsupportedWorkspaceEntry):
        await service.checkpoint(_checkpoint())

    assert ledger.calls == []


async def test_checkpoint_uploads_bodies_then_manifest_then_commits(
    service: SessionWorkspaceService,
    objects: FakeObjects,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    sandbox.scan_entries = _scan_of_manifest()
    sandbox.files["notes/a.txt"] = BODY

    result = await service.checkpoint(_checkpoint())

    assert result.status is CheckpointStatus.COMMITTED
    assert result.revision_id is not None
    # The lifecycle in design §8's order, settled only after the commit.
    assert ledger.calls == ["register", "finalizing", "ready", "commit", "settle"]

    puts = [key for kind, key in objects.calls if kind == "put"]
    copies = [key for kind, key in objects.calls if kind == "copy"]
    assert any("staging" in key and DIGEST[:16] in key for key in puts[:2])
    assert any("staging" in key and key.endswith("manifest.json") for key in puts[:2])
    assert ".index.json" in puts[2], "the durable index is written after staging"
    assert objects.calls.index(("put", puts[2])) < objects.calls.index(("copy", copies[0]))
    final_blob = blob_object(workspace_id=WORKSPACE, session_id=SESSION, digest=DIGEST).key
    assert final_blob in copies
    assert any(key.endswith(f"{result.revision_id}.json") for key in copies)


async def test_checkpoint_conflict_abandons_the_candidate(
    service: SessionWorkspaceService,
    sandbox: FakeSandbox,
    ledger: FakeLedger,
) -> None:
    sandbox.scan_entries = _scan_of_manifest()
    sandbox.files["notes/a.txt"] = BODY
    ledger.outcome = CommitOutcome(status=CheckpointStatus.CONFLICT, run=None)

    result = await service.checkpoint(_checkpoint())

    assert result.status is CheckpointStatus.CONFLICT
    assert "settle" not in ledger.calls, "a conflicted candidate is GC's to reclaim"
