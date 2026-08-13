"""The one PostgreSQL transaction of design §8 step 5, and the ledger around it.

The committer locks the Session row, compares the pointer to the base the
candidate was built on, and then moves five facts together: the immutable
revision row, the Session pointer, the Run's checkpoint marker, the slice's
tool turns and events (through the Run store, in the same session), and the
upload's `committed` mark. Between any two of those there is no observable
state — the transaction either happened or it did not.

An unknown answer is resolved the only honest way (design §5.4): re-read the
registration by `upload_id` and believe the row.
"""

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow
from tiny_hermes.session_workspace.domain.models import CheckpointStatus, UploadStatus
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow
from tiny_hermes.session_workspace.ports.store import (
    CommitCheckpoint,
    CommitOutcome,
    RegisterUpload,
    RevisionRecord,
    UploadTotals,
)

logger = logging.getLogger(__name__)


class UnknownWorkspaceSession(Exception):
    pass


class SqlWorkspaceLedger:
    """The WorkspaceLedger port over PostgreSQL, one transaction per method."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def current_revision(
        self, workspace_id: UUID, session_id: UUID
    ) -> RevisionRecord | None:
        async with self._sessions() as db:
            pointer = (
                await db.execute(
                    select(SessionRow.workspace_revision_id).where(
                        SessionRow.id == session_id,
                        SessionRow.workspace_id == workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if pointer is None:
                return None
            row = await db.get(WorkspaceRevisionRow, pointer)
            if row is None or row.workspace_id != workspace_id:
                return None
            return RevisionRecord(
                revision_id=row.id,
                manifest_object_key=row.manifest_object_key,
                manifest_sha256=row.manifest_sha256,
                manifest_schema_version=row.manifest_schema_version,
                total_bytes=row.total_bytes,
                object_count=row.object_count,
            )

    async def register_upload(self, command: RegisterUpload) -> None:
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).register_upload(command)

    async def mark_finalizing(self, upload_id: UUID, *, index_sha256: str) -> None:
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).mark_finalizing(
                upload_id, index_sha256=index_sha256
            )

    async def mark_ready(self, upload_id: UUID, *, totals: UploadTotals) -> None:
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).mark_ready(upload_id, totals=totals)

    async def abandon(self, upload_id: UUID, *, reason: str) -> None:
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).abandon(upload_id, reason=reason)

    async def settle(self, upload_id: UUID) -> None:
        async with self._sessions.begin() as db:
            await SqlWorkspaceStore(db).finish_cleanup(upload_id)

    async def commit(self, commit: CommitCheckpoint) -> CommitOutcome:
        # The §5.4 reconciliation rule runs before any state change: a retry
        # after an unknown answer must first believe the row.
        already = await self._already_committed(commit.upload_id)
        if already is not None:
            return already
        try:
            return await self._commit_once(commit)
        except DBAPIError as unknown:
            # The answer was lost, not decided. Only the row may say what
            # happened; a connection failure alone never abandons anything.
            logger.warning(
                "checkpoint commit outcome unknown; reconciling by upload_id",
                extra={"upload_id": str(commit.upload_id), "error": str(unknown)},
            )
            resolved = await self._already_committed(commit.upload_id)
            if resolved is not None:
                return resolved
            return CommitOutcome(status=CheckpointStatus.STORAGE_FAILED, run=None)

    async def _already_committed(self, upload_id: UUID) -> CommitOutcome | None:
        async with self._sessions() as db:
            row = await SqlWorkspaceStore(db).read(upload_id)
        if row is not None and row.status is UploadStatus.COMMITTED:
            return CommitOutcome(status=CheckpointStatus.COMMITTED, run=None)
        return None

    async def _commit_once(self, commit: CommitCheckpoint) -> CommitOutcome:
        async with self._sessions() as db:
            locked = (
                await db.execute(
                    select(SessionRow)
                    .where(
                        SessionRow.id == commit.session_id,
                        SessionRow.workspace_id == commit.workspace_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                raise UnknownWorkspaceSession(str(commit.session_id))

            if locked.workspace_revision_id != commit.base_revision_id:
                # Somebody else moved the pointer since this candidate's base.
                # Never last-writer-wins (design §8): nothing in this
                # transaction is kept, and the candidate is marked abandoned
                # in its own small transaction afterwards.
                await db.rollback()
                await self.abandon(commit.upload_id, reason="workspace_conflict")
                return CommitOutcome(status=CheckpointStatus.CONFLICT, run=None)

            db.add(
                WorkspaceRevisionRow(
                    id=commit.revision_id,
                    workspace_id=commit.workspace_id,
                    session_id=commit.session_id,
                    parent_revision_id=commit.base_revision_id,
                    manifest_schema_version=commit.manifest_schema_version,
                    manifest_object_key=commit.manifest_object_key,
                    manifest_sha256=commit.manifest_sha256,
                    total_bytes=commit.total_bytes,
                    object_count=commit.object_count,
                    created_by_run_id=commit.run_id,
                )
            )
            locked.workspace_revision_id = commit.revision_id

            # The transcript and the pointer move together: the Run store
            # writes the slice's turns, accounting, and state signal on this
            # same session.
            snapshot = await SqlRunStore(db).record_slice(commit.slice_command)
            await db.execute(
                update(RunRow)
                .where(RunRow.id == commit.run_id)
                .values(checkpoint_workspace_revision_id=commit.revision_id)
            )
            await SqlWorkspaceStore(db).mark_committed(
                commit.upload_id, revision_id=commit.revision_id, artifact_id=None
            )
            await db.commit()
            return CommitOutcome(status=CheckpointStatus.COMMITTED, run=snapshot)
