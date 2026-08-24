from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.infrastructure.tables import AuthIdentityRow, UserRow
from tiny_hermes.tenancy.application.workspace_service import MemberAlreadyPresent
from tiny_hermes.tenancy.domain.models import Role, Workspace, WorkspaceMember
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow, WorkspaceRow

MEMBERSHIP_CONSTRAINT = "memberships_workspace_id_user_id_key"


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
        try:
            await self._session.flush()
        except IntegrityError as error:
            if MEMBERSHIP_CONSTRAINT not in str(error.orig):
                raise
            raise MemberAlreadyPresent from error

    async def list_visible_workspaces(
        self, user_id: UUID, *, include_all: bool
    ) -> list[Workspace]:
        # Newest first: this list is a picker, and the workspace somebody
        # just created is the one they are about to open. Oldest-first put it
        # below every workspace that ever existed here.
        #
        # `id` breaks the tie in the same direction. Two workspaces can share
        # a `created_at`, and an order left to the planner shows the list in a
        # different shape on every read — worse than either order.
        statement = select(WorkspaceRow).order_by(
            WorkspaceRow.created_at.desc(), WorkspaceRow.id.desc()
        )
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
                select(MembershipRow, UserRow, AuthIdentityRow.subject)
                .join(UserRow, UserRow.id == MembershipRow.user_id)
                .join(AuthIdentityRow, AuthIdentityRow.user_id == UserRow.id)
                .where(
                    MembershipRow.workspace_id == workspace_id,
                    AuthIdentityRow.provider == "local",
                )
                .order_by(MembershipRow.created_at, MembershipRow.id)
            )
        ).all()
        return [
            WorkspaceMember(
                membership.user_id, user.display_name, subject, Role(membership.role)
            )
            for membership, user, subject in rows
        ]

    async def find_user_by_subject(self, subject: str) -> tuple[UUID, str] | None:
        row = (
            await self._session.execute(
                select(UserRow.id, UserRow.display_name)
                .join(AuthIdentityRow, AuthIdentityRow.user_id == UserRow.id)
                .where(
                    AuthIdentityRow.provider == "local",
                    AuthIdentityRow.subject == subject,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    async def update_membership(
        self, workspace_id: UUID, user_id: UUID, role: Role
    ) -> WorkspaceMember | None:
        membership = await self._session.scalar(
            select(MembershipRow)
            .where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
            .with_for_update()
        )
        if membership is None:
            return None
        membership.role = role.value
        await self._session.flush()
        members = await self.list_members(workspace_id)
        for member in members:
            if member.user_id == user_id:
                return member
        return None

    async def remove_membership(self, workspace_id: UUID, user_id: UUID) -> bool:
        membership = await self._session.scalar(
            select(MembershipRow).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        if membership is None:
            return False
        await self._session.delete(membership)
        await self._session.flush()
        return True

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
