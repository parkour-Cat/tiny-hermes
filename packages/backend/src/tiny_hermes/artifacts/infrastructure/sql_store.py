"""The ArtifactStore over PostgreSQL. Mechanics, no rules."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.artifacts.infrastructure.tables import ArtifactRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlArtifactStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, artifact: Artifact) -> None:
        self._session.add(
            ArtifactRow(
                id=artifact.id,
                workspace_id=artifact.workspace_id,
                session_id=artifact.session_id,
                run_id=artifact.run_id,
                object_key=artifact.object_key,
                filename=artifact.filename,
                media_type=artifact.media_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                truncated=artifact.truncated,
                expires_at=artifact.expires_at,
            )
        )
        await self._session.flush()

    async def read_scoped(
        self, artifact_id: UUID, workspace_id: UUID
    ) -> Artifact | None:
        row = await self._session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.workspace_id == workspace_id,
            )
        )
        return None if row is None else self._domain(row)

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def run_total_bytes(self, run_id: UUID) -> int:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(ArtifactRow.size_bytes), 0)).where(
                ArtifactRow.run_id == run_id
            )
        )
        return int(total or 0)

    async def list_for_run(self, workspace_id: UUID, run_id: UUID) -> list[Artifact]:
        rows = (
            await self._session.scalars(
                select(ArtifactRow)
                .where(
                    ArtifactRow.workspace_id == workspace_id,
                    ArtifactRow.run_id == run_id,
                )
                .order_by(ArtifactRow.created_at, ArtifactRow.id)
            )
        ).all()
        return [self._domain(row) for row in rows]

    async def expired(self, now: "datetime", limit: int) -> list[Artifact]:
        rows = await self._session.scalars(
            select(ArtifactRow)
            .where(ArtifactRow.expires_at <= now)
            .order_by(ArtifactRow.expires_at)
            .limit(limit)
        )
        return [self._domain(row) for row in rows.all()]

    async def delete(self, artifact_id: UUID) -> None:
        row = await self._session.get(ArtifactRow, artifact_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

    def _domain(self, row: ArtifactRow) -> Artifact:
        return Artifact(
            id=row.id,
            workspace_id=row.workspace_id,
            session_id=row.session_id,
            run_id=row.run_id,
            object_key=row.object_key,
            filename=row.filename,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            truncated=row.truncated,
            expires_at=row.expires_at,
        )
