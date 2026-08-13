"""Artifacts: Run output the message cannot hold, with its own rules.

Two halves. ``ArtifactService`` answers reads — always rechecking Workspace
membership, always answering cross-tenant probes with the same generic
not-found (design §6.4). ``ArtifactRecorder`` is the write half: an
``OutputSink`` whose registration exists before the first byte, so a crashed
upload is a row the collector can reclaim rather than an orphan in a bucket.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.artifacts.infrastructure.sql_store import SqlArtifactStore
from tiny_hermes.artifacts.ports.store import ArtifactStore
from tiny_hermes.session_workspace.application.cleanup import (
    CandidateIndex,
    encode_candidate_index,
)
from tiny_hermes.session_workspace.domain.models import UploadKind
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.ports.objects import (
    ObjectRef,
    ObjectStore,
    ObjectTooLarge,
    artifact_object,
    candidate_index_object,
    staging_object,
    staging_prefix_key,
)
from tiny_hermes.session_workspace.ports.store import RegisterUpload, UploadTotals
from tiny_hermes.tenancy.domain.models import Actor, Role

READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}

#: Index documents are enumerations; anything bigger is a mistake.
_INDEX_LIMIT = 1024 * 1024


class ArtifactError(Exception):
    pass


class ArtifactNotFound(ArtifactError):
    """The one answer every cross-tenant or missing-artifact probe gets."""


class ArtifactForbidden(ArtifactError):
    """The caller is not a member of the workspace they named."""


class ArtifactLimitExceeded(ArtifactError):
    """A per-artifact or per-Run ceiling refused the upload."""


class ArtifactService:
    """Reads, authorized twice: the role, then the scoped lookup."""

    def __init__(self, store: ArtifactStore, objects: ObjectStore) -> None:
        self._store = store
        self._objects = objects

    async def metadata(
        self, workspace_id: UUID, actor: Actor, artifact_id: UUID
    ) -> Artifact:
        await self._require_reader(workspace_id, actor)
        found = await self._store.read_scoped(artifact_id, workspace_id)
        if found is None or found.expires_at <= datetime.now(UTC):
            # Missing, expired, and other-tenant all sound exactly the same.
            raise ArtifactNotFound
        return found

    def content(self, artifact: Artifact) -> AsyncIterator[bytes]:
        """The bytes, streamed. Call ``metadata`` first — that is the check."""
        return self._objects.get_stream(ObjectRef(key=artifact.object_key))

    async def _require_reader(self, workspace_id: UUID, actor: Actor) -> None:
        role = await self._store.role_for(workspace_id, actor.id)
        if role is not None:
            if role not in READERS:
                raise ArtifactForbidden
            return
        if not actor.is_platform_admin:
            raise ArtifactForbidden


@dataclass(frozen=True)
class ArtifactLimits:
    artifact_max_bytes: int
    run_artifact_max_bytes: int
    retention_seconds: int
    staging_ttl_seconds: int


class ArtifactRecorder:
    """One command's output, spilling past the preview into object storage.

    Implements the engine's ``OutputSink``: the registration row and its
    durable candidate index key exist before the first byte, the body streams
    to staging without ever being whole in memory, and the artifacts row is
    inserted in the same transaction that marks the upload committed.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        objects: ObjectStore,
        workspace_id: UUID,
        session_id: UUID,
        run_id: UUID,
        filename: str,
        media_type: str,
        limits: ArtifactLimits,
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._run_id = run_id
        self._filename = filename
        self._media_type = media_type
        self._limits = limits
        self._artifact_id = uuid.uuid4()
        #: Public: the registration's identifier is how callers and tests ask
        #: the upload lifecycle what happened.
        self.upload_id = uuid.uuid4()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
        self._uploader: asyncio.Task[object] | None = None
        self._registration: RegisterUpload | None = None
        self.delivered = 0

    @property
    def artifact_limit(self) -> int:
        return self._limits.artifact_max_bytes

    async def deliver(self, chunk: bytes) -> None:
        if self._uploader is None:
            await self._begin()
        self.delivered += len(chunk)
        await self._queue.put(chunk)

    async def _begin(self) -> None:
        """The row before the byte (design §6.2), then the streaming task."""
        async with self._sessions() as db:
            existing = await SqlArtifactStore(db).run_total_bytes(self._run_id)
        if existing >= self._limits.run_artifact_max_bytes:
            raise ArtifactLimitExceeded("the run's artifact budget is spent")

        registration = RegisterUpload(
            upload_id=self.upload_id,
            kind=UploadKind.ARTIFACT,
            workspace_id=self._workspace_id,
            session_id=self._session_id,
            run_id=self._run_id,
            base_revision_id=None,
            candidate_revision_id=None,
            candidate_artifact_id=self._artifact_id,
            staging_prefix=staging_prefix_key(
                workspace_id=self._workspace_id,
                session_id=self._session_id,
                upload_id=self.upload_id,
            ),
            candidate_index_key=candidate_index_object(
                workspace_id=self._workspace_id,
                session_id=self._session_id,
                upload_id=self.upload_id,
            ).key,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self._limits.staging_ttl_seconds),
        )
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).register_upload(registration)
        self._registration = registration

        # The remaining per-Run room also caps this stream: the ceiling is
        # enforced while bytes arrive, not discovered at commit.
        room = min(
            self._limits.artifact_max_bytes,
            self._limits.run_artifact_max_bytes
            - await self._existing_bytes(),
        )
        self._uploader = asyncio.create_task(
            self._objects.put_stream(
                self._staging_ref(), _drain(self._queue), limit_bytes=max(room, 1)
            )
        )

    async def _existing_bytes(self) -> int:
        async with self._sessions() as db:
            return await SqlArtifactStore(db).run_total_bytes(self._run_id)

    def _staging_ref(self) -> ObjectRef:
        return staging_object(
            workspace_id=self._workspace_id,
            session_id=self._session_id,
            upload_id=self.upload_id,
            name="artifact",
        )

    async def finish(self, *, truncated: bool) -> Artifact | None:
        """Finalize per the upload chain, or leave the debt for the collector.

        Returns None when the command produced nothing worth keeping.
        """
        if self._uploader is None:
            return None
        await self._queue.put(None)
        try:
            stored = await self._uploader
        except ObjectTooLarge as over:
            await self._abandon("artifact_over_limit")
            raise ArtifactLimitExceeded(str(over)) from over
        except Exception:
            await self._abandon("artifact_upload_failed")
            raise

        size = int(getattr(stored, "size", 0))
        digest = str(getattr(stored, "sha256", ""))
        final = artifact_object(
            workspace_id=self._workspace_id,
            run_id=self._run_id,
            artifact_id=self._artifact_id,
        )
        registration = self._registration
        if registration is None:  # pragma: no cover - deliver() precedes finish()
            return None

        index = encode_candidate_index(
            CandidateIndex(upload_id=self.upload_id, final_keys=(final.key,))
        )
        stored_index = await self._objects.put_stream(
            ObjectRef(key=registration.candidate_index_key),
            _once(index),
            limit_bytes=_INDEX_LIMIT,
        )
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).mark_finalizing(
                self.upload_id, index_sha256=stored_index.sha256
            )
        await self._objects.server_copy(self._staging_ref(), final)
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).mark_ready(
                self.upload_id,
                totals=UploadTotals(
                    total_bytes=size, object_count=1, final_object_key=final.key
                ),
            )

        artifact = Artifact(
            id=self._artifact_id,
            workspace_id=self._workspace_id,
            session_id=self._session_id,
            run_id=self._run_id,
            object_key=final.key,
            filename=self._filename,
            media_type=self._media_type,
            size_bytes=size,
            sha256=digest,
            truncated=truncated,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self._limits.retention_seconds),
        )
        # The row and the committed mark move together, or not at all.
        async with self._sessions() as db:
            await SqlArtifactStore(db).insert(artifact)
            await SqlWorkspaceStore(db).mark_committed(
                self.upload_id, revision_id=None, artifact_id=self._artifact_id
            )
            await db.commit()

        await self._settle(registration)
        return artifact

    async def _abandon(self, reason: str) -> None:
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).abandon(self.upload_id, reason=reason)

    async def _settle(self, registration: RegisterUpload) -> None:
        """Post-commit staging cleanup; a failure only leaves the flag set."""
        try:
            await self._objects.delete_many([self._staging_ref()])
            await self._objects.delete_many(
                [ObjectRef(key=registration.candidate_index_key)]
            )
            async with self._sessions.begin() as db:
                await SqlWorkspaceStore(db).finish_cleanup(self.upload_id)
        except Exception:  # noqa: BLE001 - the debt stays recorded for GC
            return


async def _drain(queue: "asyncio.Queue[bytes | None]") -> AsyncIterator[bytes]:
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data
