from typing import Protocol
from uuid import UUID

from tiny_hermes.tenancy.domain.models import Role, Workspace, WorkspaceMember


class WorkspaceStore(Protocol):
    async def create_workspace(self, name: str) -> Workspace: ...

    async def add_membership(self, workspace_id: UUID, user_id: UUID, role: Role) -> None: ...

    async def list_visible_workspaces(
        self, user_id: UUID, *, include_all: bool
    ) -> list[Workspace]: ...

    async def get_membership(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]: ...

    async def find_user_by_subject(
        self, subject: str
    ) -> tuple[UUID, str] | None: ...

    async def update_membership(
        self, workspace_id: UUID, user_id: UUID, role: Role
    ) -> WorkspaceMember | None: ...

    async def remove_membership(self, workspace_id: UUID, user_id: UUID) -> bool: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
    ) -> None: ...
