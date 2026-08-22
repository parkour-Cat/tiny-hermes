from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.domain.models import (
    AuthenticatedUser,
    StoredIdentity,
    StoredSession,
)
from tiny_hermes.identity.infrastructure.tables import (
    AuthIdentityRow,
    AuthSessionRow,
    UserRow,
)


class SqlAuthStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def bootstrap_lock(self) -> AsyncGenerator[None]:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": 847_263_991}
        )
        yield

    async def has_platform_admin(self) -> bool:
        value = await self._session.scalar(
            select(UserRow.id).where(UserRow.is_platform_admin.is_(True)).limit(1)
        )
        return value is not None

    async def create_platform_admin(
        self, subject: str, display_name: str, password_hash: str
    ) -> AuthenticatedUser:
        user = UserRow(
            status="active",
            display_name=display_name,
            is_platform_admin=True,
        )
        self._session.add(user)
        await self._session.flush()
        self._session.add(
            AuthIdentityRow(
                user_id=user.id,
                provider="local",
                subject=subject,
                password_hash=password_hash,
            )
        )
        return AuthenticatedUser(
            user.id,
            subject,
            user.display_name,
            user.status,
            user.is_platform_admin,
        )

    async def find_local_identity(self, subject: str) -> StoredIdentity | None:
        result = await self._session.execute(
            select(UserRow, AuthIdentityRow)
            .join(AuthIdentityRow, AuthIdentityRow.user_id == UserRow.id)
            .where(AuthIdentityRow.provider == "local", AuthIdentityRow.subject == subject)
        )
        row = result.one_or_none()
        if row is None:
            return None
        user, identity = row[0], row[1]
        return StoredIdentity(self._to_user(user, identity.subject), identity.password_hash)

    async def find_oidc_identity(self, subject: str) -> StoredIdentity | None:
        result = await self._session.execute(
            select(UserRow, AuthIdentityRow)
            .join(AuthIdentityRow, AuthIdentityRow.user_id == UserRow.id)
            .where(AuthIdentityRow.provider == "oidc", AuthIdentityRow.subject == subject)
        )
        row = result.one_or_none()
        if row is None:
            return None
        user, identity = row[0], row[1]
        return StoredIdentity(self._to_user(user, identity.subject), identity.password_hash)

    async def create_oidc_user(self, subject: str, display_name: str) -> AuthenticatedUser:
        user = UserRow(
            status="active",
            display_name=display_name,
            is_platform_admin=False,
        )
        self._session.add(user)
        await self._session.flush()
        self._session.add(
            AuthIdentityRow(
                user_id=user.id,
                provider="oidc",
                subject=subject,
                password_hash=None,
            )
        )
        return AuthenticatedUser(
            user.id,
            subject,
            user.display_name,
            user.status,
            user.is_platform_admin,
        )

    async def create_session(
        self,
        user_id: UUID,
        token_digest: str,
        csrf_digest: str,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            AuthSessionRow(
                user_id=user_id,
                token_digest=token_digest,
                csrf_digest=csrf_digest,
                expires_at=expires_at,
                revoked_at=None,
            )
        )

    async def find_session(self, token_digest: str, now: datetime) -> StoredSession | None:
        # Not `.join(AuthIdentityRow, ... provider == "local")`: that hard-coded
        # filter meant an OIDC-authenticated session could never be re-found by
        # `AuthService.authenticate`/`verify_csrf`, silently breaking OIDC
        # login design §2's own requirement — "the same session cookie ...
        # through the same code path". A scalar subquery instead of a second
        # join keeps this to one row per session even though a User may in
        # principle carry more than one `AuthIdentityRow` (§353): which
        # identity minted a given session is not recorded anywhere, so this
        # picks the oldest deterministically rather than raising on however
        # many identities a future admin-console binding feature creates.
        identity_subject = (
            select(AuthIdentityRow.subject)
            .where(AuthIdentityRow.user_id == UserRow.id)
            .order_by(AuthIdentityRow.created_at)
            .limit(1)
            .correlate(UserRow)
            .scalar_subquery()
        )
        result = await self._session.execute(
            select(AuthSessionRow, UserRow, identity_subject)
            .join(UserRow, UserRow.id == AuthSessionRow.user_id)
            .where(AuthSessionRow.token_digest == token_digest, AuthSessionRow.expires_at > now)
        )
        row = result.one_or_none()
        if row is None:
            return None
        auth_session, user, subject = row[0], row[1], row[2]
        if subject is None:
            return None
        return StoredSession(
            self._to_user(user, subject),
            auth_session.csrf_digest,
            auth_session.expires_at,
            auth_session.revoked_at,
        )

    async def revoke_session(self, token_digest: str, now: datetime) -> bool:
        row = await self._session.scalar(
            select(AuthSessionRow).where(AuthSessionRow.token_digest == token_digest)
        )
        if row is None:
            return False
        row.revoked_at = now
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
        self._session.add(
            AuditEventRow(
                workspace_id=None,
                actor_type="user" if actor_id else "anonymous",
                actor_id=actor_id,
                action=action,
                resource_type="identity",
                resource_id=actor_id,
                result=result,
                request_id=request_id,
                context=context,
            )
        )

    @staticmethod
    def _to_user(user: UserRow, subject: str) -> AuthenticatedUser:
        return AuthenticatedUser(
            user.id,
            subject,
            user.display_name,
            user.status,
            user.is_platform_admin,
        )
