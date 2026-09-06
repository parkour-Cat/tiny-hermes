"""Read saved files after the caller has authorized the Run or Session.

No request supplies an object key. A file path must name an entry in the
verified manifest; storage keys derive only from that entry's digest and scope.
"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from tiny_hermes.session_workspace.application.service import (
    MANIFEST_LIMIT_BYTES,
    WorkspaceIntegrityFailed,
    parse_saved_manifest,
)
from tiny_hermes.session_workspace.domain.models import EntryType, WorkspaceEntry
from tiny_hermes.session_workspace.ports.files import FileSource, WorkspaceFileStore
from tiny_hermes.session_workspace.ports.objects import (
    ObjectMissing,
    ObjectRef,
    ObjectStorageUnavailable,
    ObjectStore,
    blob_object,
)


class WorkspaceFileNotFound(Exception):
    pass


class WorkspaceFilesChanged(Exception):
    pass


class WorkspaceFilesUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SavedFiles:
    revision_id: UUID | None
    items: tuple[WorkspaceEntry, ...]


class WorkspaceFiles:
    def __init__(self, store: WorkspaceFileStore, objects: ObjectStore) -> None:
        self.store = store
        self._objects = objects

    async def list_files(self, source: FileSource | None) -> SavedFiles:
        if source is None:
            raise WorkspaceFileNotFound
        if source.missing_revision:
            raise WorkspaceFilesUnavailable
        record = source.revision
        if record is None:
            return SavedFiles(None, ())
        try:
            data = bytearray()
            async for chunk in self._objects.get_stream(ObjectRef(key=record.manifest_object_key)):
                data.extend(chunk)
                if len(data) > MANIFEST_LIMIT_BYTES:
                    raise WorkspaceFilesUnavailable
            if hashlib.sha256(data).hexdigest() != record.manifest_sha256:
                raise WorkspaceFilesUnavailable
            manifest = parse_saved_manifest(bytes(data))
            if manifest.schema_version != record.manifest_schema_version:
                raise WorkspaceFilesUnavailable
        except (ObjectMissing, ObjectStorageUnavailable, WorkspaceIntegrityFailed) as error:
            raise WorkspaceFilesUnavailable from error
        return SavedFiles(
            record.revision_id,
            tuple(entry for entry in manifest.entries if entry.entry_type is EntryType.FILE),
        )

    async def content(
        self,
        source: FileSource | None,
        revision_id: UUID,
        path: str,
    ) -> tuple[WorkspaceEntry, AsyncIterator[bytes]]:
        snapshot = await self.list_files(source)
        if snapshot.revision_id != revision_id:
            raise WorkspaceFilesChanged
        entry = next((entry for entry in snapshot.items if entry.path == path), None)
        if entry is None or source is None or entry.sha256 is None:
            raise WorkspaceFileNotFound
        ref = blob_object(
            workspace_id=source.workspace_id, session_id=source.session_id, digest=entry.sha256
        )
        try:
            stat = await self._objects.stat(ref)
        except (ObjectMissing, ObjectStorageUnavailable) as error:
            raise WorkspaceFilesUnavailable from error
        if stat is None or stat.size != entry.size:
            raise WorkspaceFilesUnavailable
        return entry, self._stream(ref, entry)

    async def _stream(self, ref: ObjectRef, entry: WorkspaceEntry) -> AsyncIterator[bytes]:
        received = 0
        digest = hashlib.sha256()
        async for chunk in self._objects.get_stream(ref):
            received += len(chunk)
            if received > entry.size:
                raise WorkspaceFilesUnavailable
            digest.update(chunk)
            yield chunk
        if received != entry.size or digest.hexdigest() != entry.sha256:
            raise WorkspaceFilesUnavailable
