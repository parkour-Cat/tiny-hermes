from uuid import UUID

from tiny_hermes.tenancy.domain.models import Actor, Role, Workspace, WorkspaceMember
from tiny_hermes.tenancy.ports.store import WorkspaceStore

WRITERS = {Role.WORKSPACE_ADMIN}
READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}


class Forbidden(Exception):
    pass


class InvalidWorkspaceName(Exception):
    pass


class UnknownUser(Exception):
    pass


class UnknownMember(Exception):
    pass


class MemberAlreadyPresent(Exception):
    pass


class LastWorkspaceAdmin(Exception):
    pass


class InvalidMemberRole(Exception):
    pass


class WorkspaceService:
    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store

    async def create_workspace(self, actor: Actor, name: str, request_id: str) -> Workspace:
        if not actor.is_platform_admin:
            raise Forbidden
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise InvalidWorkspaceName
        workspace = await self._store.create_workspace(normalized)
        await self._store.add_membership(workspace.id, actor.id, Role.WORKSPACE_ADMIN)
        await self._store.append_audit(
            workspace_id=workspace.id,
            actor_id=actor.id,
            action="workspace.created",
            resource_id=workspace.id,
            request_id=request_id,
        )
        return workspace

    async def list_workspaces(self, actor: Actor) -> list[Workspace]:
        return await self._store.list_visible_workspaces(
            actor.id, include_all=actor.is_platform_admin
        )

    async def list_members(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> list[WorkspaceMember]:
        platform = await self._require_member(actor, workspace_id)
        if platform:
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="workspace.members_read_by_platform_admin",
                resource_id=workspace_id,
                request_id=request_id,
            )
        return await self._store.list_members(workspace_id)

    async def invite_member(
        self,
        actor: Actor,
        workspace_id: UUID,
        email: str,
        role: Role,
        request_id: str,
    ) -> WorkspaceMember:
        platform = await self._require_admin(actor, workspace_id)
        _valid_member_role(role)
        found = await self._store.find_user_by_subject(email.strip().lower())
        if found is None:
            raise UnknownUser
        user_id, display_name = found
        if await self._store.get_membership(workspace_id, user_id) is not None:
            raise MemberAlreadyPresent
        await self._store.add_membership(workspace_id, user_id, role)
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=(
                "workspace.member_invited_by_platform_admin"
                if platform
                else "workspace.member_invited"
            ),
            resource_id=user_id,
            request_id=request_id,
        )
        return WorkspaceMember(user_id, display_name, email.strip().lower(), role)

    async def change_member_role(
        self,
        actor: Actor,
        workspace_id: UUID,
        user_id: UUID,
        role: Role,
        request_id: str,
    ) -> WorkspaceMember:
        platform = await self._require_admin(actor, workspace_id)
        _valid_member_role(role)
        current = await self._store.get_membership(workspace_id, user_id)
        if current is None:
            raise UnknownMember
        if current is Role.WORKSPACE_ADMIN and role is not Role.WORKSPACE_ADMIN:
            await self._refuse_last_admin(workspace_id)
        updated = await self._store.update_membership(workspace_id, user_id, role)
        if updated is None:
            raise UnknownMember
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=(
                "workspace.member_role_changed_by_platform_admin"
                if platform
                else "workspace.member_role_changed"
            ),
            resource_id=user_id,
            request_id=request_id,
        )
        return updated

    async def remove_member(
        self, actor: Actor, workspace_id: UUID, user_id: UUID, request_id: str
    ) -> None:
        platform = await self._require_admin(actor, workspace_id)
        current = await self._store.get_membership(workspace_id, user_id)
        if current is None:
            raise UnknownMember
        if current is Role.WORKSPACE_ADMIN:
            await self._refuse_last_admin(workspace_id)
        removed = await self._store.remove_membership(workspace_id, user_id)
        if not removed:
            raise UnknownMember
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=(
                "workspace.member_removed_by_platform_admin"
                if platform
                else "workspace.member_removed"
            ),
            resource_id=user_id,
            request_id=request_id,
        )

    async def my_role(self, actor: Actor, workspace_id: UUID) -> str:
        """这个人在这个工作空间里是什么角色。

        不走 `_require_member`：那一条问的是「能不能读这个工作空间」，而这里问
        的是「你是谁」。两者今天的答案恰好相同，但它们会各自演化——把「我是谁」
        建立在「我能读吗」之上，等于让一次权限收紧顺手改掉一个人的身份。

        平台管理员而非成员时回 `platform_admin`。它不是一个 workspace 角色，
        但控制台需要知道这个人能看见一切；伪装成 `workspace_admin` 会让页面
        显示一个他在这个工作空间里并不拥有的身份。
        """
        role = await self._store.get_membership(workspace_id, actor.id)
        if role is not None:
            return role.value
        if actor.is_platform_admin:
            return "platform_admin"
        raise Forbidden

    async def _require_member(self, actor: Actor, workspace_id: UUID) -> bool:
        role = await self._store.get_membership(workspace_id, actor.id)
        if role is not None:
            if role not in READERS:
                raise Forbidden
            return False
        if not actor.is_platform_admin:
            raise Forbidden
        return True

    async def _require_admin(self, actor: Actor, workspace_id: UUID) -> bool:
        role = await self._store.get_membership(workspace_id, actor.id)
        if role is not None:
            if role not in WRITERS:
                raise Forbidden
            return False
        if not actor.is_platform_admin:
            raise Forbidden
        return True

    async def _refuse_last_admin(self, workspace_id: UUID) -> None:
        members = await self._store.list_members(workspace_id)
        if sum(1 for member in members if member.role is Role.WORKSPACE_ADMIN) <= 1:
            raise LastWorkspaceAdmin


def _valid_member_role(role: Role) -> None:
    if role not in READERS:
        raise InvalidMemberRole
