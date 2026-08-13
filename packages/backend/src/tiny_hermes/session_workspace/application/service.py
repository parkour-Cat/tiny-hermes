"""The deep module: callers see restore and checkpoint, and nothing else.

Design §5.1. The manifest arithmetic, the object staging, the upload
lifecycle, and the sandbox streams are all behind these two methods. Both
flows are deliberate about order — what is verified before what moves, and
what is persisted before what exists — because every ordering here is a
recovery story somewhere else.
"""

import hashlib
import json
import tarfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from tiny_hermes.session_workspace.application.cleanup import (
    CandidateIndex,
    encode_candidate_index,
)
from tiny_hermes.session_workspace.domain.manifest import QuotaMeasurement, build_manifest, measure
from tiny_hermes.session_workspace.domain.models import (
    CheckpointStatus,
    EntryType,
    WorkspaceEntry,
    WorkspaceManifest,
    WorkspaceQuota,
)
from tiny_hermes.session_workspace.ports.objects import (
    ObjectMissing,
    ObjectRef,
    ObjectStore,
    blob_object,
    candidate_index_object,
    manifest_object,
    staging_object,
    staging_prefix_key,
)
from tiny_hermes.session_workspace.ports.sandbox import SandboxWorkspacePort, ScanEntry
from tiny_hermes.session_workspace.ports.store import (
    CommitCheckpoint,
    RegisterUpload,
    RevisionRecord,
    UploadKind,
    UploadTotals,
    WorkspaceLedger,
)

MANIFEST_SCHEMA_VERSION = 1
#: A manifest is metadata; far beyond this it is not a manifest but a mistake.
MANIFEST_LIMIT_BYTES = 128 * 1024 * 1024
#: The uid baked into the sandbox image; restored files belong to it.
SANDBOX_UID = 10001


class WorkspaceIntegrityFailed(Exception):
    """Stored state that fails verification. Design §7: the Run fails, an
    operator repairs; this is never retried into a lucky success."""


@dataclass(frozen=True)
class WorkspaceRestore:
    workspace_id: UUID
    session_id: UUID
    run_id: UUID


@dataclass(frozen=True)
class WorkspaceCheckpoint:
    workspace_id: UUID
    session_id: UUID
    run_id: UUID
    base_revision_id: UUID | None
    quota: WorkspaceQuota
    #: The Run module's RecordSliceCommand, persisted with the revision in one
    #: transaction. Opaque here.
    slice_command: Any


@dataclass(frozen=True)
class RestoreResult:
    revision_id: UUID | None
    total_bytes: int
    object_count: int


@dataclass(frozen=True)
class CheckpointResult:
    status: CheckpointStatus
    revision_id: UUID | None = None
    measurement: QuotaMeasurement | None = None
    run: Any | None = None


class SessionWorkspaceService:
    def __init__(
        self,
        *,
        ledger: WorkspaceLedger,
        objects: ObjectStore,
        sandbox: SandboxWorkspacePort,
        staging_ttl_seconds: int,
    ) -> None:
        self._ledger = ledger
        self._objects = objects
        self._sandbox = sandbox
        self._staging_ttl = timedelta(seconds=staging_ttl_seconds)

    # -- restore ---------------------------------------------------------------

    async def restore(self, command: WorkspaceRestore) -> RestoreResult:
        """Design §7: verify, stream, re-scan, compare — in that order."""
        record = await self._ledger.current_revision(
            command.workspace_id, command.session_id
        )
        if record is None:
            # An empty workspace needs no object call and no import.
            return RestoreResult(revision_id=None, total_bytes=0, object_count=0)

        manifest = await self._verified_manifest(command, record)
        await self._sandbox.import_tree(
            self._restore_tar(command, manifest),
            declared_total=_tar_total(manifest),
        )

        restored = _manifest_of_scan(await self._sandbox.scan())
        if restored.canonical_bytes() != manifest.canonical_bytes():
            raise WorkspaceIntegrityFailed(
                "the restored tree does not equal the manifest"
            )
        return RestoreResult(
            revision_id=record.revision_id,
            total_bytes=manifest.total_bytes,
            object_count=manifest.object_count,
        )

    async def _verified_manifest(
        self, command: WorkspaceRestore, record: RevisionRecord
    ) -> WorkspaceManifest:
        try:
            data = await self._read_bounded(record.manifest_object_key)
        except ObjectMissing as gone:
            raise WorkspaceIntegrityFailed(f"manifest object missing: {gone}") from gone
        if hashlib.sha256(data).hexdigest() != record.manifest_sha256:
            raise WorkspaceIntegrityFailed("manifest bytes fail their recorded hash")
        manifest = _parse_manifest(data)
        if manifest.schema_version != record.manifest_schema_version:
            raise WorkspaceIntegrityFailed("manifest schema differs from the record")
        del command
        return manifest

    async def _read_bounded(self, key: str) -> bytes:
        received = b""
        async for chunk in self._objects.get_stream(ObjectRef(key=key)):
            received += chunk
            if len(received) > MANIFEST_LIMIT_BYTES:
                raise WorkspaceIntegrityFailed("manifest larger than any honest manifest")
        return received

    async def _restore_tar(
        self, command: WorkspaceRestore, manifest: WorkspaceManifest
    ) -> AsyncIterator[bytes]:
        """The exact tree as tar bytes, hashed while it streams.

        Manifest order is parent-before-child by construction (bytewise path
        order), so no member arrives before its directory.
        """
        for entry in manifest.entries:
            yield _tar_header(entry)
            if entry.entry_type is not EntryType.FILE:
                continue
            ref = blob_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                digest=entry.sha256 or "",
            )
            hasher = hashlib.sha256()
            sent = 0
            try:
                async for chunk in self._objects.get_stream(ref):
                    hasher.update(chunk)
                    sent += len(chunk)
                    yield chunk
            except ObjectMissing as gone:
                raise WorkspaceIntegrityFailed(f"blob missing: {entry.path}") from gone
            if sent != entry.size or hasher.hexdigest() != entry.sha256:
                # Detected mid-stream: the import aborts and the sandbox is
                # dirtied rather than left plausibly restored.
                raise WorkspaceIntegrityFailed(f"blob fails its digest: {entry.path}")
            yield _padding(sent)
        yield b"\0" * 1024

    # -- checkpoint --------------------------------------------------------------

    async def checkpoint(self, command: WorkspaceCheckpoint) -> CheckpointResult:
        """Design §8: measure, stage, finalize, place, then one transaction."""
        manifest = _manifest_of_scan(await self._sandbox.scan())

        measurement = measure(manifest, command.quota)
        if not measurement.allowed:
            # Refused before a single body is uploaded; rollback is the
            # worker's next move (design §9).
            return CheckpointResult(
                status=CheckpointStatus.LIMIT_EXCEEDED, measurement=measurement
            )

        base = await self._base_manifest(command)
        if base is not None and manifest.sha256 == base.sha256:
            return CheckpointResult(
                status=CheckpointStatus.UNCHANGED,
                revision_id=command.base_revision_id,
                measurement=measurement,
            )

        upload_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        registration = RegisterUpload(
            upload_id=upload_id,
            kind=UploadKind.WORKSPACE,
            workspace_id=command.workspace_id,
            session_id=command.session_id,
            run_id=command.run_id,
            base_revision_id=command.base_revision_id,
            candidate_revision_id=revision_id,
            candidate_artifact_id=None,
            staging_prefix=staging_prefix_key(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                upload_id=upload_id,
            ),
            candidate_index_key=candidate_index_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                upload_id=upload_id,
            ).key,
            expires_at=datetime.now(UTC) + self._staging_ttl,
        )
        # The row exists before any object: that ordering is what lets GC
        # tell orphaned garbage from a commit in flight (design §6.2).
        await self._ledger.register_upload(registration)

        staged_digests = await self._stage_bodies(command, manifest, base, upload_id)
        manifest_bytes = manifest.canonical_bytes()
        staged_manifest = staging_object(
            workspace_id=command.workspace_id,
            session_id=command.session_id,
            upload_id=upload_id,
            name="manifest.json",
        )
        await self._objects.put_stream(
            staged_manifest, _once(manifest_bytes), limit_bytes=MANIFEST_LIMIT_BYTES
        )

        final_manifest = manifest_object(
            workspace_id=command.workspace_id,
            session_id=command.session_id,
            revision_id=revision_id,
        )
        final_keys = [
            blob_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                digest=digest,
            ).key
            for digest in sorted(staged_digests)
        ] + [final_manifest.key]

        index_bytes = encode_candidate_index(
            CandidateIndex(upload_id=upload_id, final_keys=tuple(final_keys))
        )
        stored_index = await self._objects.put_stream(
            ObjectRef(key=registration.candidate_index_key),
            _once(index_bytes),
            limit_bytes=MANIFEST_LIMIT_BYTES,
        )
        if await self._objects.stat(ObjectRef(key=registration.candidate_index_key)) is None:
            raise WorkspaceIntegrityFailed("the candidate index did not persist")
        await self._ledger.mark_finalizing(upload_id, index_sha256=stored_index.sha256)

        await self._place_final_objects(
            command, upload_id, staged_digests, staged_manifest, final_manifest
        )
        await self._ledger.mark_ready(
            upload_id,
            totals=UploadTotals(
                total_bytes=manifest.total_bytes, object_count=manifest.object_count
            ),
        )

        outcome = await self._ledger.commit(
            CommitCheckpoint(
                upload_id=upload_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                run_id=command.run_id,
                base_revision_id=command.base_revision_id,
                revision_id=revision_id,
                manifest_object_key=final_manifest.key,
                manifest_sha256=manifest.sha256,
                manifest_schema_version=MANIFEST_SCHEMA_VERSION,
                total_bytes=manifest.total_bytes,
                object_count=manifest.object_count,
                slice_command=command.slice_command,
            )
        )
        if outcome.status is not CheckpointStatus.COMMITTED:
            # A conflicted or unknown candidate is the collector's to reclaim;
            # deleting anything here could race a concurrent truth.
            return CheckpointResult(status=outcome.status, measurement=measurement)

        await self._reclaim_staging(registration, upload_id)
        return CheckpointResult(
            status=CheckpointStatus.COMMITTED,
            revision_id=revision_id,
            measurement=measurement,
            run=outcome.run,
        )

    async def _base_manifest(
        self, command: WorkspaceCheckpoint
    ) -> WorkspaceManifest | None:
        if command.base_revision_id is None:
            return None
        record = await self._ledger.current_revision(
            command.workspace_id, command.session_id
        )
        if record is None or record.revision_id != command.base_revision_id:
            # The pointer moved under us. The commit transaction is the
            # authority on conflicts; here it only disables dedup arithmetic.
            return None
        try:
            data = await self._read_bounded(record.manifest_object_key)
        except ObjectMissing as gone:
            raise WorkspaceIntegrityFailed(f"base manifest missing: {gone}") from gone
        if hashlib.sha256(data).hexdigest() != record.manifest_sha256:
            raise WorkspaceIntegrityFailed("base manifest fails its recorded hash")
        return _parse_manifest(data)

    async def _stage_bodies(
        self,
        command: WorkspaceCheckpoint,
        manifest: WorkspaceManifest,
        base: WorkspaceManifest | None,
        upload_id: UUID,
    ) -> set[str]:
        """Upload every body the Session's blob space does not already hold."""
        base_digests: set[str] = (
            set()
            if base is None
            else {entry.sha256 for entry in base.entries if entry.sha256}
        )
        wanted: dict[str, WorkspaceEntry] = {}
        for entry in manifest.entries:
            if entry.sha256 and entry.sha256 not in base_digests:
                wanted.setdefault(entry.sha256, entry)

        needed: dict[str, WorkspaceEntry] = {}
        for digest, entry in wanted.items():
            final = blob_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                digest=digest,
            )
            # Dedup inside this Session only (design §6.5): a blob an earlier
            # revision already placed is referenced, not re-uploaded.
            if await self._objects.stat(final) is None:
                needed[digest] = entry

        by_path = {entry.path: digest for digest, entry in needed.items()}
        staged: set[str] = set()
        async for exported in self._sandbox.export_files(sorted(by_path)):
            digest = by_path[exported.path]
            target = staging_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                upload_id=upload_id,
                name=f"blob-{digest}",
            )
            stored = await self._objects.put_stream(
                target, exported.chunks, limit_bytes=needed[digest].size
            )
            if stored.sha256 != digest:
                raise WorkspaceIntegrityFailed(
                    f"exported body fails its scanned digest: {exported.path}"
                )
            staged.add(digest)
        if staged != set(needed):
            raise WorkspaceIntegrityFailed("the export skipped a needed body")
        return staged

    async def _place_final_objects(
        self,
        command: WorkspaceCheckpoint,
        upload_id: UUID,
        staged_digests: set[str],
        staged_manifest: ObjectRef,
        final_manifest: ObjectRef,
    ) -> None:
        for digest in sorted(staged_digests):
            source = staging_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                upload_id=upload_id,
                name=f"blob-{digest}",
            )
            target = blob_object(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                digest=digest,
            )
            await self._objects.server_copy(source, target)
            if await self._objects.stat(target) is None:
                raise WorkspaceIntegrityFailed(f"final blob did not persist: {digest[:16]}")
        await self._objects.server_copy(staged_manifest, final_manifest)
        if await self._objects.stat(final_manifest) is None:
            raise WorkspaceIntegrityFailed("final manifest did not persist")

    async def _reclaim_staging(
        self, registration: RegisterUpload, upload_id: UUID
    ) -> None:
        """Post-commit cleanup. A failure only leaves `cleanup_pending` set."""
        try:
            while True:
                batch = await self._objects.list_prefix(
                    registration.staging_prefix, limit=1000
                )
                if not batch:
                    break
                await self._objects.delete_many(batch)
            await self._objects.delete_many(
                [ObjectRef(key=registration.candidate_index_key)]
            )
            await self._ledger.settle(upload_id)
        except Exception:  # noqa: BLE001 - the debt stays recorded for GC
            return


# -- pure helpers ----------------------------------------------------------------


def _manifest_of_scan(entries: tuple[ScanEntry, ...]) -> WorkspaceManifest:
    """The scan judged: unsupported entry types are refused right here."""
    judged = tuple(
        WorkspaceEntry(
            path=entry.path,
            entry_type=EntryType.from_scan(entry.entry_type),
            mode=entry.mode,
            size=entry.size if entry.entry_type == "file" else 0,
            sha256=entry.sha256 if entry.entry_type == "file" else None,
        )
        for entry in entries
    )
    return build_manifest(judged, schema_version=MANIFEST_SCHEMA_VERSION)


def _parse_manifest(data: bytes) -> WorkspaceManifest:
    try:
        parsed: Any = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as broken:
        raise WorkspaceIntegrityFailed("manifest is not a JSON document") from broken
    if not isinstance(parsed, dict):
        raise WorkspaceIntegrityFailed("manifest is not a JSON object")
    document = cast(dict[str, Any], parsed)
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise WorkspaceIntegrityFailed("manifest entries are not a list")
    try:
        entries = tuple(
            WorkspaceEntry(
                path=str(item["path"]),
                entry_type=EntryType(str(item["type"])),
                mode=int(item["mode"]),
                size=int(item["size"]),
                sha256=None if item["sha256"] is None else str(item["sha256"]),
            )
            for item in cast(list[dict[str, Any]], raw_entries)
        )
        manifest = build_manifest(
            entries, schema_version=int(document.get("schema_version", 0))
        )
    except Exception as broken:
        raise WorkspaceIntegrityFailed(f"manifest fails validation: {broken}") from broken
    if manifest.total_bytes != int(document.get("total_bytes", -1)) or (
        manifest.object_count != int(document.get("object_count", -1))
    ):
        raise WorkspaceIntegrityFailed("manifest totals disagree with its entries")
    return manifest


def _tar_header(entry: WorkspaceEntry) -> bytes:
    info = tarfile.TarInfo(entry.path)
    info.mode = entry.mode
    info.uid = SANDBOX_UID
    info.gid = SANDBOX_UID
    if entry.entry_type is EntryType.DIRECTORY:
        info.type = tarfile.DIRTYPE
    else:
        info.type = tarfile.REGTYPE
        info.size = entry.size
    return info.tobuf(format=tarfile.GNU_FORMAT)


def _padding(size: int) -> bytes:
    remainder = size % 512
    return b"" if remainder == 0 else b"\0" * (512 - remainder)


def _tar_total(manifest: WorkspaceManifest) -> int:
    total = 1024  # the end-of-archive marker
    for entry in manifest.entries:
        total += len(_tar_header(entry))
        if entry.entry_type is EntryType.FILE:
            total += entry.size + len(_padding(entry.size))
    return total


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data
