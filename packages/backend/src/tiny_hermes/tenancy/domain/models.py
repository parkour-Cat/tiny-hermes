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
    #: Task-9 review finding C/D: an end user, dressed up as an `Actor` so
    #: `SubjectService`/`ApprovalService` need no separate code path for
    #: "acting on my own data" (`end_user_subject_routes.py::_self`,
    #: `end_user_approval_routes.py`'s own `Actor(caller.end_user_id, ...)`).
    #: `id` alone cannot say which table it indexes — `users` or
    #: `end_users`, two different namespaces that happen to share a UUID
    #: type — so callers that need to tell an end user apart from a
    #: workspace member (an audit row's own `actor_type`; whether an
    #: `actor.id` match against a Run's `end_user_id` means what it looks
    #: like it means) read this flag instead of comparing ids alone. Same
    #: shape as `is_service_account`, one line up, and the same reason: a
    #: fact about which kind of subject this `Actor` stands in for, not a
    #: role or a scope.
    is_end_user: bool = False
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
