from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4


class Role(StrEnum):
    WORKSPACE_ADMIN = "workspace_admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Actor:
    id: UUID
    is_platform_admin: bool

    @classmethod
    def new(cls, is_platform_admin: bool) -> "Actor":
        return cls(id=uuid4(), is_platform_admin=is_platform_admin)


@dataclass(frozen=True)
class Workspace:
    id: UUID
    name: str
    status: str


@dataclass(frozen=True)
class WorkspaceMember:
    user_id: UUID
    display_name: str
    role: Role
