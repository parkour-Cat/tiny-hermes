"""The collector's reference oracle, computed from retained manifests.

Design §13: blob references are calculated from the manifests the platform
still retains, and an uncertain reference keeps the blob. M1 retains every
committed revision row, so "referenced" means "named by any of this Session's
revision manifests" — and any failure to read or verify a manifest makes
every blob answer UNCERTAIN, because a collector that guesses deletes other
people's data.
"""

import hashlib
import json
import logging
import re
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.artifacts.infrastructure.tables import ArtifactRow
from tiny_hermes.session_workspace.application.cleanup import Reference
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow
from tiny_hermes.session_workspace.ports.objects import ObjectRef, ObjectStore

logger = logging.getLogger(__name__)

_MANIFEST_KEY = re.compile(r"/manifests/([0-9a-f-]{36})\.json$")
_BLOB_KEY = re.compile(r"/blobs/sha256/([0-9a-f]{64})$")
_ARTIFACT_KEY = re.compile(r"/artifacts/[0-9a-f-]{36}$")
_MANIFEST_CAP = 128 * 1024 * 1024


class RetainedManifestOracle:
    """Answers for one Session's keys; built per reclaim batch."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        objects: ObjectStore,
        workspace_id: UUID,
        session_id: UUID,
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._digests: set[str] | None = None
        self._uncertain = False

    async def referenced(self, key: str) -> Reference:
        manifest = _MANIFEST_KEY.search(key)
        if manifest is not None:
            async with self._sessions() as db:
                row = await db.get(WorkspaceRevisionRow, UUID(manifest.group(1)))
            return Reference.REFERENCED if row is not None else Reference.UNREFERENCED

        if _ARTIFACT_KEY.search(key) is not None:
            async with self._sessions() as db:
                found = await db.scalar(
                    select(ArtifactRow.id).where(ArtifactRow.object_key == key)
                )
            return Reference.REFERENCED if found is not None else Reference.UNREFERENCED

        blob = _BLOB_KEY.search(key)
        if blob is None:
            # A key the collector cannot classify is a key it must not touch.
            return Reference.UNCERTAIN
        await self._load_digests()
        if self._uncertain:
            return Reference.UNCERTAIN
        digests = self._digests or set()
        return Reference.REFERENCED if blob.group(1) in digests else Reference.UNREFERENCED

    async def _load_digests(self) -> None:
        if self._digests is not None or self._uncertain:
            return
        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(
                        WorkspaceRevisionRow.manifest_object_key,
                        WorkspaceRevisionRow.manifest_sha256,
                    ).where(
                        WorkspaceRevisionRow.workspace_id == self._workspace_id,
                        WorkspaceRevisionRow.session_id == self._session_id,
                    )
                )
            ).all()
        digests: set[str] = set()
        for key, recorded_sha in rows:
            try:
                data = await self._read_bounded(str(key))
            except Exception:  # noqa: BLE001 - unreadable manifest means keep
                logger.warning("manifest unreadable during gc", extra={"key": str(key)})
                self._uncertain = True
                return
            if hashlib.sha256(data).hexdigest() != str(recorded_sha):
                logger.error("manifest fails its hash during gc", extra={"key": str(key)})
                self._uncertain = True
                return
            digests.update(_digests_of(data))
        self._digests = digests

    async def _read_bounded(self, key: str) -> bytes:
        received = b""
        async for chunk in self._objects.get_stream(ObjectRef(key=key)):
            received += chunk
            if len(received) > _MANIFEST_CAP:
                raise ValueError("manifest larger than any honest manifest")
        return received


def _digests_of(manifest_bytes: bytes) -> set[str]:
    parsed: Any = json.loads(manifest_bytes)
    entries = cast(list[dict[str, Any]], parsed.get("entries", []))
    return {
        str(entry["sha256"]) for entry in entries if entry.get("sha256") is not None
    }
