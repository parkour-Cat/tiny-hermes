"""ObjectUpload transitions as guarded UPDATEs.

Every mutation puts its expected prior state into the WHERE clause and reads
the row back from RETURNING. Zero rows updated means the guard failed; only
then is the row re-read to say *how* — unknown, or in a different state. Two
processes can race any transition and the database, not luck, picks the one
winner.
"""

from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.session_workspace.domain.models import UploadKind, UploadStatus
from tiny_hermes.session_workspace.infrastructure.tables import ObjectUploadRow
from tiny_hermes.session_workspace.ports.store import (
    ObjectUpload,
    RegisterUpload,
    UnknownUpload,
    UploadStateConflict,
    UploadTotals,
)

LIVE = (
    UploadStatus.UPLOADING.value,
    UploadStatus.FINALIZING.value,
    UploadStatus.READY.value,
)


def _snapshot(row: ObjectUploadRow) -> ObjectUpload:
    return ObjectUpload(
        upload_id=row.id,
        kind=UploadKind(row.kind),
        workspace_id=row.workspace_id,
        session_id=row.session_id,
        run_id=row.run_id,
        base_revision_id=row.base_revision_id,
        candidate_revision_id=row.candidate_revision_id,
        candidate_artifact_id=row.candidate_artifact_id,
        staging_prefix=row.staging_prefix,
        candidate_index_key=row.candidate_index_key,
        candidate_index_sha256=row.candidate_index_sha256,
        final_object_key=row.final_object_key,
        status=UploadStatus(row.status),
        cleanup_pending=row.cleanup_pending,
        total_bytes=row.total_bytes,
        object_count=row.object_count,
        committed_revision_id=row.committed_revision_id,
        abandon_reason=row.abandon_reason,
        expires_at=row.expires_at,
    )


class SqlWorkspaceStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read(self, upload_id: UUID) -> ObjectUpload | None:
        row = await self._session.get(
            ObjectUploadRow, upload_id, populate_existing=True
        )
        return None if row is None else _snapshot(row)

    async def register_upload(self, command: RegisterUpload) -> ObjectUpload:
        row = ObjectUploadRow(
            id=command.upload_id,
            kind=command.kind.value,
            workspace_id=command.workspace_id,
            session_id=command.session_id,
            run_id=command.run_id,
            base_revision_id=command.base_revision_id,
            candidate_revision_id=command.candidate_revision_id,
            candidate_artifact_id=command.candidate_artifact_id,
            staging_prefix=command.staging_prefix,
            candidate_index_key=command.candidate_index_key,
            status=UploadStatus.UPLOADING.value,
            # Staging is about to exist; the debt is recorded before the bytes.
            cleanup_pending=True,
            expires_at=command.expires_at,
        )
        self._session.add(row)
        # Flushed here so a duplicate key is refused at the call, not at the
        # end of a transaction that has since done other work.
        await self._session.flush()
        return _snapshot(row)

    async def mark_finalizing(
        self, upload_id: UUID, *, index_sha256: str
    ) -> ObjectUpload:
        row = await self._transition(
            upload_id,
            expected=(UploadStatus.UPLOADING.value,),
            status=UploadStatus.FINALIZING.value,
            candidate_index_sha256=index_sha256,
        )
        return _snapshot(row)

    async def mark_ready(self, upload_id: UUID, *, totals: UploadTotals) -> ObjectUpload:
        row = await self._transition(
            upload_id,
            expected=(UploadStatus.FINALIZING.value,),
            status=UploadStatus.READY.value,
            total_bytes=totals.total_bytes,
            object_count=totals.object_count,
            final_object_key=totals.final_object_key,
        )
        return _snapshot(row)

    async def mark_committed(
        self, upload_id: UUID, *, revision_id: UUID | None, artifact_id: UUID | None
    ) -> None:
        outcome = await self._session.execute(
            update(ObjectUploadRow)
            .where(
                ObjectUploadRow.id == upload_id,
                ObjectUploadRow.status == UploadStatus.READY.value,
                # Commit confirms the registered candidate, nothing else.
                ObjectUploadRow.candidate_revision_id.is_not_distinct_from(revision_id),
                ObjectUploadRow.candidate_artifact_id.is_not_distinct_from(artifact_id),
            )
            .values(
                status=UploadStatus.COMMITTED.value,
                committed_revision_id=revision_id,
                updated_at=datetime.now(UTC),
            )
            .returning(ObjectUploadRow.id)
        )
        if outcome.scalar_one_or_none() is None:
            await self._refused(upload_id)

    async def abandon(self, upload_id: UUID, *, reason: str) -> None:
        await self._transition(
            upload_id,
            expected=LIVE,
            status=UploadStatus.ABANDONED.value,
            abandon_reason=reason,
            cleanup_pending=True,
        )

    async def claim_cleanup(
        self, now: datetime, *, limit: int
    ) -> tuple[ObjectUpload, ...]:
        settled_debt = and_(
            ObjectUploadRow.status.in_(
                (UploadStatus.ABANDONED.value, UploadStatus.COMMITTED.value)
            ),
            ObjectUploadRow.cleanup_pending,
        )
        timed_out = and_(
            ObjectUploadRow.status.in_(LIVE), ObjectUploadRow.expires_at <= now
        )
        found = await self._session.execute(
            select(ObjectUploadRow)
            .where(or_(settled_debt, timed_out))
            .order_by(ObjectUploadRow.updated_at)
            .limit(limit)
        )
        return tuple(_snapshot(row) for row in found.scalars())

    async def finish_cleanup(self, upload_id: UUID) -> None:
        outcome = await self._session.execute(
            update(ObjectUploadRow)
            .where(
                ObjectUploadRow.id == upload_id,
                # A row whose debt is already clear was finished by someone
                # else; this caller's claim is stale and must hear about it.
                ObjectUploadRow.cleanup_pending,
            )
            .values(
                status=case(
                    (
                        ObjectUploadRow.status == UploadStatus.COMMITTED.value,
                        UploadStatus.COMMITTED.value,
                    ),
                    else_=UploadStatus.EXPIRED.value,
                ),
                cleanup_pending=False,
                updated_at=datetime.now(UTC),
            )
            .returning(ObjectUploadRow.id)
        )
        if outcome.scalar_one_or_none() is None:
            await self._refused(upload_id)

    async def _transition(
        self, upload_id: UUID, *, expected: tuple[str, ...], **values: object
    ) -> ObjectUploadRow:
        outcome = await self._session.execute(
            update(ObjectUploadRow)
            .where(ObjectUploadRow.id == upload_id, ObjectUploadRow.status.in_(expected))
            .values(updated_at=datetime.now(UTC), **values)
            .returning(ObjectUploadRow)
        )
        row = outcome.scalar_one_or_none()
        if row is None:
            await self._refused(upload_id)
        return row

    async def _refused(self, upload_id: UUID) -> NoReturn:
        """Turn a failed guard into the truth about why it failed."""
        current = await self._session.get(
            ObjectUploadRow, upload_id, populate_existing=True
        )
        if current is None:
            raise UnknownUpload(str(upload_id))
        raise UploadStateConflict(upload_id, UploadStatus(current.status))
