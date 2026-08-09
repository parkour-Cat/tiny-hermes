from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.identity.domain.models import AuthenticatedUser, StoredIdentity, StoredSession


class AuthStore(Protocol):
    def bootstrap_lock(self) -> AbstractAsyncContextManager[None]: ...

    async def has_platform_admin(self) -> bool: ...

    async def create_platform_admin(
        self, subject: str, display_name: str, password_hash: str
    ) -> AuthenticatedUser: ...

    async def find_local_identity(self, subject: str) -> StoredIdentity | None: ...

    async def create_session(
        self,
        user_id: UUID,
        token_digest: str,
        csrf_digest: str,
        expires_at: datetime,
    ) -> None: ...

    async def find_session(self, token_digest: str, now: datetime) -> StoredSession | None: ...

    async def revoke_session(self, token_digest: str, now: datetime) -> bool: ...

    async def append_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        result: str,
        request_id: str,
        context: dict[str, str],
    ) -> None: ...
