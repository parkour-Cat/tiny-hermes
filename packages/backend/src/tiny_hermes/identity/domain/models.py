from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tiny_hermes.tenancy.domain.models import Role


@dataclass(frozen=True)
class NewLocalUser:
    subject: str
    display_name: str
    password: str


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    subject: str
    display_name: str
    status: str
    is_platform_admin: bool


@dataclass(frozen=True)
class StoredIdentity:
    user: AuthenticatedUser
    password_hash: str


@dataclass(frozen=True)
class StoredSession:
    user: AuthenticatedUser
    csrf_digest: str
    expires_at: datetime
    revoked_at: datetime | None


class ServiceAccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ChannelIssuerStatus(StrEnum):
    """Design §4.3: disabling a row invalidates that issuer's *new* credentials
    immediately. A session already exchanged is unaffected until it is
    separately revoked — a real trade documented there, not a gap here."""

    ACTIVE = "active"
    DISABLED = "disabled"


API_KEY_SCOPES = frozenset({"runs.read", "runs.write", "runs.control", "agents.read"})
TOKEN_PREFIX = "thk_"  # noqa: S105 - public token prefix, not a secret
TOKEN_PREFIX_LENGTH = 8

ROLE_SCOPES: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset({"runs.read", "agents.read"}),
    Role.DEVELOPER: frozenset({"runs.read", "runs.write", "runs.control", "agents.read"}),
}

MACHINE_ROLES = frozenset({Role.DEVELOPER, Role.VIEWER})


def scopes_for_role(role: Role) -> frozenset[str]:
    return ROLE_SCOPES.get(role, frozenset())


@dataclass(frozen=True)
class ServiceAccount:
    id: UUID
    workspace_id: UUID
    name: str
    role: Role
    status: ServiceAccountStatus
    created_by_user_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class ApiKey:
    id: UUID
    service_account_id: UUID
    prefix: str
    scopes: tuple[str, ...]
    agent_ids: tuple[UUID, ...]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class IssuedApiKey:
    key: ApiKey
    token: str


@dataclass(frozen=True)
class AuthenticatedMachine:
    account: ServiceAccount
    key: ApiKey
    scopes: frozenset[str]
