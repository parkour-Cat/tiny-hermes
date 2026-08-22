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

    async def find_oidc_identity(self, subject: str) -> StoredIdentity | None:
        """`subject` is the IdP's own `sub` claim, never an email. OIDC login
        design's red line: a lookup miss must lead to a new User, never to a
        `find_local_identity` fallback keyed on the same claim's `email`."""
        ...

    async def create_oidc_user(self, subject: str, display_name: str) -> AuthenticatedUser:
        """A brand new User plus one `AuthIdentityRow(provider='oidc', ...)`,
        with no password. Called only after `find_oidc_identity` missed —
        never as a way to attach a second identity to an existing User."""
        ...

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
