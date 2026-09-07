from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow
from tiny_hermes.session_workspace.ports.files import FileSource
from tiny_hermes.session_workspace.ports.store import RevisionRecord


class SqlWorkspaceFiles:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def run_source(self, workspace_id: UUID, run_id: UUID) -> FileSource | None:
        row = await self._db.scalar(
            select(RunRow).where(
                RunRow.workspace_id == workspace_id,
                RunRow.id == run_id,
            )
        )
        if row is None:
            return None
        return await self._source(
            workspace_id, row.session_id, row.checkpoint_workspace_revision_id
        )

    async def session_source(self, workspace_id: UUID, session_id: UUID) -> FileSource | None:
        row = await self._db.scalar(
            select(SessionRow).where(
                SessionRow.workspace_id == workspace_id,
                SessionRow.id == session_id,
            )
        )
        if row is None:
            return None
        return await self._source(workspace_id, session_id, row.workspace_revision_id)

    async def _source(
        self,
        workspace_id: UUID,
        session_id: UUID,
        revision_id: UUID | None,
    ) -> FileSource:
        row = (
            None
            if revision_id is None
            else await self._db.scalar(
                select(WorkspaceRevisionRow).where(
                    WorkspaceRevisionRow.id == revision_id,
                    WorkspaceRevisionRow.workspace_id == workspace_id,
                    WorkspaceRevisionRow.session_id == session_id,
                )
            )
        )
        revision = (
            None
            if row is None
            else RevisionRecord(
                revision_id=row.id,
                manifest_object_key=row.manifest_object_key,
                manifest_sha256=row.manifest_sha256,
                manifest_schema_version=row.manifest_schema_version,
                total_bytes=row.total_bytes,
                object_count=row.object_count,
            )
        )
        return FileSource(
            workspace_id,
            session_id,
            revision,
            missing_revision=revision_id is not None and row is None,
        )
