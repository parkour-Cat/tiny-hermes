from uuid import UUID, uuid4

from tiny_hermes.tenancy.domain.models import Role, Workspace, WorkspaceMember


class MemoryWorkspaceStore:
    def __init__(self) -> None:
        self._workspaces: dict[UUID, Workspace] = {}
        self._memberships: dict[tuple[UUID, UUID], Role] = {}
        self._people: dict[UUID, tuple[str, str]] = {}
        self.audit_actions: list[str] = []

    def register_person(self, user_id: UUID, display_name: str, subject: str) -> None:
        self._people[user_id] = (display_name, subject)

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
            self._member(user_id, role)
            for (member_workspace_id, user_id), role in self._memberships.items()
            if member_workspace_id == workspace_id
        ]

    async def find_user_by_subject(self, subject: str) -> tuple[UUID, str] | None:
        wanted = subject.strip().lower()
        for user_id, (display_name, email) in self._people.items():
            if email == wanted:
                return user_id, display_name
        return None

    async def update_membership(
        self, workspace_id: UUID, user_id: UUID, role: Role
    ) -> WorkspaceMember | None:
        if (workspace_id, user_id) not in self._memberships:
            return None
        self._memberships[(workspace_id, user_id)] = role
        return self._member(user_id, role)

    async def remove_membership(self, workspace_id: UUID, user_id: UUID) -> bool:
        return self._memberships.pop((workspace_id, user_id), None) is not None

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

    def _member(self, user_id: UUID, role: Role) -> WorkspaceMember:
        display_name, subject = self._people.get(user_id, (str(user_id), ""))
        return WorkspaceMember(user_id, display_name, subject, role)
