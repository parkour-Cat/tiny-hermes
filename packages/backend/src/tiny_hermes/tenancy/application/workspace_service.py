from uuid import UUID

from tiny_hermes.tenancy.domain.models import Actor, Role, Workspace, WorkspaceMember
from tiny_hermes.tenancy.ports.store import WorkspaceStore


class Forbidden(Exception):
    pass


class InvalidWorkspaceName(Exception):
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
        role = await self._store.get_membership(workspace_id, actor.id)
        if role is None:
            if not actor.is_platform_admin:
                raise Forbidden
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="workspace.members_read_by_platform_admin",
                resource_id=workspace_id,
                request_id=request_id,
            )
        return await self._store.list_members(workspace_id)
