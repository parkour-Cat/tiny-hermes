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
    #: Service accounts are not Users. Their id is the account id, their role
    #: is stored on the account rather than looked up in memberships, and a
    #: key's scopes (already intersected with that role) hang here so a route
    #: can refuse without a second database round-trip.
    is_service_account: bool = False
    role: Role | None = None
    scopes: frozenset[str] | None = None

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
    subject: str
    role: Role
