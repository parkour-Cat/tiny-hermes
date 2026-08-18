"""The MCP catalog, shaped after `http_tools/infrastructure/sql_store.py`.

The one thing worth reading twice is the same one: the same snapshot is the
same version, decided by the content hash and enforced by a unique index rather
than by a check the service has to remember to run first.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.mcp.application.service import VersionResult
from tiny_hermes.mcp.domain.models import (
    McpServer,
    McpServerStatus,
    McpServerVersion,
)
from tiny_hermes.mcp.infrastructure.tables import McpServerRow, McpServerVersionRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow
from tiny_hermes.tools.domain.mcp import McpCapabilities, McpTool


class SqlMcpStore:
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

    async def create_server(
        self,
        *,
        workspace_id: UUID,
        name: str,
        url: str,
        credential_ref: str | None,
        created_by: UUID,
    ) -> McpServer:
        now = datetime.now(UTC)
        row = McpServerRow(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            url=url,
            credential_ref=credential_ref,
            last_validated_at=now,
            created_by=created_by,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as clash:
            await self._session.rollback()
            # The service turns this into its own refusal, so the store stays
            # free of the service's vocabulary.
            raise ValueError(f"a server named {name!r} already exists here") from clash
        return _server(row)

    async def get_server(self, server_id: UUID) -> McpServer | None:
        row = await self._session.get(McpServerRow, server_id)
        return None if row is None else _server(row)

    async def list_servers(self, workspace_id: UUID) -> Sequence[McpServer]:
        rows = (
            await self._session.scalars(
                select(McpServerRow)
                .where(McpServerRow.workspace_id == workspace_id)
                .order_by(McpServerRow.name)
            )
        ).all()
        return [_server(row) for row in rows]

    async def add_version(
        self,
        *,
        mcp_server_id: UUID,
        capabilities: McpCapabilities,
        created_by: UUID,
    ) -> VersionResult:
        existing = await self._session.scalar(
            select(McpServerVersionRow).where(
                McpServerVersionRow.mcp_server_id == mcp_server_id,
                McpServerVersionRow.content_hash == capabilities.content_hash,
            )
        )
        await self.mark_validated(mcp_server_id, datetime.now(UTC))
        if existing is not None:
            return VersionResult(version=_version(existing), created=False)
        highest = await self._session.scalar(
            select(func.max(McpServerVersionRow.version_number)).where(
                McpServerVersionRow.mcp_server_id == mcp_server_id
            )
        )
        row = McpServerVersionRow(
            id=uuid4(),
            mcp_server_id=mcp_server_id,
            version_number=int(highest or 0) + 1,
            content_hash=capabilities.content_hash,
            tools=[_tool_document(tool) for tool in capabilities.tools],
            status=McpServerStatus.ACTIVE.value,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return VersionResult(version=_version(row), created=True)

    async def get_version(self, version_id: UUID) -> McpServerVersion | None:
        row = await self._session.get(McpServerVersionRow, version_id)
        return None if row is None else _version(row)

    async def list_versions(self, server_id: UUID) -> Sequence[McpServerVersion]:
        rows = (
            await self._session.scalars(
                select(McpServerVersionRow)
                .where(McpServerVersionRow.mcp_server_id == server_id)
                .order_by(McpServerVersionRow.version_number)
            )
        ).all()
        return [_version(row) for row in rows]

    async def set_version_status(
        self, version_id: UUID, status: McpServerStatus
    ) -> McpServerVersion | None:
        row = await self._session.get(McpServerVersionRow, version_id)
        if row is None:
            return None
        row.status = status.value
        await self._session.flush()
        return _version(row)

    async def set_current_version(
        self, server_id: UUID, version_id: UUID | None
    ) -> McpServer | None:
        row = await self._session.get(McpServerRow, server_id)
        if row is None:
            return None
        row.current_version_id = version_id
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _server(row)

    async def mark_validated(self, server_id: UUID, moment: datetime) -> None:
        row = await self._session.get(McpServerRow, server_id)
        if row is None:  # pragma: no cover - the caller holds one
            return
        row.last_validated_at = moment
        await self._session.flush()

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
                resource_type="mcp_server",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )
        await self._session.flush()


def _server(row: McpServerRow) -> McpServer:
    return McpServer(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        url=row.url,
        credential_ref=row.credential_ref,
        current_version_id=row.current_version_id,
        last_validated_at=row.last_validated_at,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version(row: McpServerVersionRow) -> McpServerVersion:
    return McpServerVersion(
        id=row.id,
        mcp_server_id=row.mcp_server_id,
        version_number=row.version_number,
        content_hash=row.content_hash,
        tools=tuple(_tool(entry) for entry in row.tools),
        status=McpServerStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _tool_document(tool: McpTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _tool(entry: dict[str, Any]) -> McpTool:
    description = entry.get("description")
    return McpTool(
        name=str(entry["name"]),
        description=None if description is None else str(description),
        input_schema=cast(dict[str, Any], entry.get("input_schema") or {}),
    )
