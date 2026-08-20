"""The HTTP tool catalog, one operation at a time.

Shaped after `skills/infrastructure/sql_store.py`. The one thing worth reading
twice is `add_version`: the same document is the same version, decided by the
content hash and enforced by a unique index rather than by a check the service
has to remember to run first.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.http_tools.application.service import VersionResult
from tiny_hermes.http_tools.domain.models import (
    HttpTool,
    HttpToolStatus,
    HttpToolVersion,
)
from tiny_hermes.http_tools.infrastructure.documents import (
    operation_document,
    operation_from_document,
)
from tiny_hermes.http_tools.infrastructure.tables import HttpToolRow, HttpToolVersionRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow
from tiny_hermes.tools.domain.openapi import parse_document


class SqlHttpToolStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def create_tool(
        self,
        *,
        workspace_id: UUID,
        name: str,
        base_url: str,
        credential_ref: str | None,
        created_by: UUID,
    ) -> HttpTool:
        now = datetime.now(UTC)
        row = HttpToolRow(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            base_url=base_url,
            credential_ref=credential_ref,
            created_by=created_by,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as clash:
            await self._session.rollback()
            # The service turns this into its own refusal. Raised as a plain
            # `ValueError` so the store stays free of the service's vocabulary.
            raise ValueError(f"a tool named {name!r} already exists here") from clash
        return _tool(row)

    async def get_tool(self, tool_id: UUID) -> HttpTool | None:
        row = await self._session.get(HttpToolRow, tool_id)
        return None if row is None else _tool(row)

    async def list_tools(self, workspace_id: UUID) -> Sequence[HttpTool]:
        rows = (
            await self._session.scalars(
                select(HttpToolRow)
                .where(HttpToolRow.workspace_id == workspace_id)
                .order_by(HttpToolRow.name)
            )
        ).all()
        return [_tool(row) for row in rows]

    async def add_version(
        self, *, http_tool_id: UUID, document: str, created_by: UUID
    ) -> VersionResult:
        parsed = parse_document(document)
        existing = await self._session.scalar(
            select(HttpToolVersionRow).where(
                HttpToolVersionRow.http_tool_id == http_tool_id,
                HttpToolVersionRow.content_hash == parsed.content_hash,
            )
        )
        if existing is not None:
            return VersionResult(version=_version(existing), created=False)
        highest = await self._session.scalar(
            select(func.max(HttpToolVersionRow.version_number)).where(
                HttpToolVersionRow.http_tool_id == http_tool_id
            )
        )
        row = HttpToolVersionRow(
            id=uuid4(),
            http_tool_id=http_tool_id,
            version_number=int(highest or 0) + 1,
            content_hash=parsed.content_hash,
            title=parsed.title,
            document_version=parsed.version,
            operations=[operation_document(item) for item in parsed.operations],
            document=document,
            status=HttpToolStatus.ACTIVE.value,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return VersionResult(version=_version(row), created=True)

    async def get_version(self, version_id: UUID) -> HttpToolVersion | None:
        row = await self._session.get(HttpToolVersionRow, version_id)
        return None if row is None else _version(row)

    async def list_versions(self, tool_id: UUID) -> Sequence[HttpToolVersion]:
        rows = (
            await self._session.scalars(
                select(HttpToolVersionRow)
                .where(HttpToolVersionRow.http_tool_id == tool_id)
                .order_by(HttpToolVersionRow.version_number)
            )
        ).all()
        return [_version(row) for row in rows]

    async def read_document(self, version_id: UUID) -> str | None:
        return await self._session.scalar(
            select(HttpToolVersionRow.document).where(
                HttpToolVersionRow.id == version_id
            )
        )

    async def set_version_status(
        self, version_id: UUID, status: HttpToolStatus
    ) -> HttpToolVersion | None:
        row = await self._session.get(HttpToolVersionRow, version_id)
        if row is None:
            return None
        row.status = status.value
        await self._session.flush()
        return _version(row)

    async def set_current_version(
        self, tool_id: UUID, version_id: UUID | None
    ) -> HttpTool | None:
        row = await self._session.get(HttpToolRow, tool_id)
        if row is None:
            return None
        row.current_version_id = version_id
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _tool(row)

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        self._session.add(
            AuditEventRow(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="http_tool",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )
        await self._session.flush()


def _tool(row: HttpToolRow) -> HttpTool:
    return HttpTool(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        base_url=row.base_url,
        credential_ref=row.credential_ref,
        current_version_id=row.current_version_id,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version(row: HttpToolVersionRow) -> HttpToolVersion:
    return HttpToolVersion(
        id=row.id,
        http_tool_id=row.http_tool_id,
        version_number=row.version_number,
        content_hash=row.content_hash,
        title=row.title,
        document_version=row.document_version,
        operations=tuple(operation_from_document(entry) for entry in row.operations),
        status=HttpToolStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
    )
