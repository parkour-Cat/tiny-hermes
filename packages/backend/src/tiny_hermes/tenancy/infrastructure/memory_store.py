from uuid import UUID, uuid4

from tiny_hermes.tenancy.domain.models import Role, Workspace, WorkspaceMember


class MemoryWorkspaceStore:
    def __init__(self) -> None:
        self._workspaces: dict[UUID, Workspace] = {}
        self._memberships: dict[tuple[UUID, UUID], Role] = {}
        self.audit_actions: list[str] = []

    async def create_workspace(self, name: str) -> Workspace:
        workspace = Workspace(uuid4(), name, "active")
        self._workspaces[workspace.id] = workspace
        return workspace

    async def add_membership(self, workspace_id: UUID, user_id: UUID, role: Role) -> None:
        self._memberships[(workspace_id, user_id)] = role

    async def list_visible_workspaces(
        self, user_id: UUID, *, include_all: bool
    ) -> list[Workspace]:
        if include_all:
            return list(self._workspaces.values())
        visible_ids = {
            workspace_id
            for workspace_id, member_id in self._memberships
            if member_id == user_id
        }
        return [workspace for workspace in self._workspaces.values() if workspace.id in visible_ids]

    async def get_membership(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self._memberships.get((workspace_id, user_id))

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        return [
            WorkspaceMember(user_id, str(user_id), role)
            for (member_workspace_id, user_id), role in self._memberships.items()
            if member_workspace_id == workspace_id
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
        del workspace_id, actor_id, resource_id, request_id
        self.audit_actions.append(action)

    def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self._memberships.get((workspace_id, user_id))
