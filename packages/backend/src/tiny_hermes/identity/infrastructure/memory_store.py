import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

from tiny_hermes.identity.domain.models import (
    AuthenticatedUser,
    StoredIdentity,
    StoredSession,
)


class MemoryAuthStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._identities: dict[str, StoredIdentity] = {}
        self._users: dict[UUID, AuthenticatedUser] = {}
        self._sessions: dict[str, StoredSession] = {}
        self.audit_actions: list[str] = []

    @property
    def session_token_digests(self) -> set[str]:
        return set(self._sessions)

    @property
    def csrf_token_digests(self) -> set[str]:
        return {stored.csrf_digest for stored in self._sessions.values()}

    @asynccontextmanager
    async def bootstrap_lock(self) -> AsyncGenerator[None]:
        async with self._lock:
            yield

    async def has_platform_admin(self) -> bool:
        return any(user.is_platform_admin for user in self._users.values())

    async def create_platform_admin(
        self, subject: str, display_name: str, password_hash: str
    ) -> AuthenticatedUser:
        user = AuthenticatedUser(uuid4(), subject, display_name, "active", True)
        self._users[user.id] = user
        self._identities[subject] = StoredIdentity(user, password_hash)
        return user

    async def find_local_identity(self, subject: str) -> StoredIdentity | None:
        return self._identities.get(subject)

    async def create_session(
        self,
        user_id: UUID,
        token_digest: str,
        csrf_digest: str,
        expires_at: datetime,
    ) -> None:
        self._sessions[token_digest] = StoredSession(
            self._users[user_id], csrf_digest, expires_at, None
        )

    async def find_session(self, token_digest: str, now: datetime) -> StoredSession | None:
        stored = self._sessions.get(token_digest)
        if stored is None or stored.expires_at <= now:
            return None
        return stored

    async def revoke_session(self, token_digest: str, now: datetime) -> bool:
        stored = self._sessions.get(token_digest)
        if stored is None:
            return False
        self._sessions[token_digest] = StoredSession(
            stored.user, stored.csrf_digest, stored.expires_at, now
        )
        return True

    async def append_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        result: str,
        request_id: str,
        context: dict[str, str],
    ) -> None:
        del actor_id, result, request_id, context
        self.audit_actions.append(action)
