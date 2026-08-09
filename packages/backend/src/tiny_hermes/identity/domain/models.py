from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


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
