from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.infrastructure.tables import UserRow
from tiny_hermes.tenancy.domain.models import Role, Workspace, WorkspaceMember
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow, WorkspaceRow


class SqlWorkspaceStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_workspace(self, name: str) -> Workspace:
        row = WorkspaceRow(name=name, status="active")
        self._session.add(row)
        await self._session.flush()
        return Workspace(row.id, row.name, row.status)

    async def add_membership(self, workspace_id: UUID, user_id: UUID, role: Role) -> None:
        self._session.add(
            MembershipRow(workspace_id=workspace_id, user_id=user_id, role=role.value)
        )

    async def list_visible_workspaces(
        self, user_id: UUID, *, include_all: bool
    ) -> list[Workspace]:
        statement = select(WorkspaceRow).order_by(WorkspaceRow.created_at, WorkspaceRow.id)
        if not include_all:
            statement = statement.join(
                MembershipRow, MembershipRow.workspace_id == WorkspaceRow.id
            ).where(MembershipRow.user_id == user_id)
        rows = (await self._session.scalars(statement)).all()
        return [Workspace(row.id, row.name, row.status) for row in rows]

    async def get_membership(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        rows = (
            await self._session.execute(
                select(MembershipRow, UserRow)
                .join(UserRow, UserRow.id == MembershipRow.user_id)
                .where(MembershipRow.workspace_id == workspace_id)
                .order_by(MembershipRow.created_at, MembershipRow.id)
            )
        ).all()
        return [
            WorkspaceMember(membership.user_id, user.display_name, Role(membership.role))
            for membership, user in rows
        ]

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
    ) -> None:
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="workspace",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context={},
            )
        )
