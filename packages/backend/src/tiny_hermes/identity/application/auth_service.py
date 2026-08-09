import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash

from tiny_hermes.identity.domain.models import AuthenticatedUser, NewLocalUser
from tiny_hermes.identity.ports.store import AuthStore


class BootstrapClosed(Exception):
    pass


class InvalidBootstrapToken(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class AuthService:
    def __init__(self, store: AuthStore, bootstrap_token: str, session_ttl_seconds: int) -> None:
        self._store = store
        self._bootstrap_token = bootstrap_token
        self._session_ttl = timedelta(seconds=session_ttl_seconds)
        self._passwords = PasswordHash.recommended()

    async def bootstrap(
        self, presented_token: str, command: NewLocalUser, request_id: str
    ) -> AuthenticatedUser:
        if not hmac.compare_digest(presented_token, self._bootstrap_token):
            await self._store.append_audit(
                actor_id=None,
                action="identity.bootstrap_failed",
                result="denied",
                request_id=request_id,
                context={"reason": "invalid_token"},
            )
            raise InvalidBootstrapToken
        async with self._store.bootstrap_lock():
            if await self._store.has_platform_admin():
                raise BootstrapClosed
            user = await self._store.create_platform_admin(
                command.subject.strip().lower(),
                command.display_name.strip(),
                self._passwords.hash(command.password),
            )
            await self._store.append_audit(
                actor_id=user.id,
                action="identity.bootstrap_succeeded",
                result="succeeded",
                request_id=request_id,
                context={},
            )
            return user

    async def login(
        self, subject: str, password: str, request_id: str
    ) -> tuple[str, str, AuthenticatedUser]:
        identity = await self._store.find_local_identity(subject.strip().lower())
        if identity is None or not self._passwords.verify(password, identity.password_hash):
            await self._store.append_audit(
                actor_id=None,
                action="identity.login_failed",
                result="denied",
                request_id=request_id,
                context={},
            )
            raise InvalidCredentials
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        await self._store.create_session(
            identity.user.id,
            self.digest_token(session_token),
            self.digest_token(csrf_token),
            datetime.now(UTC) + self._session_ttl,
        )
        await self._store.append_audit(
            actor_id=identity.user.id,
            action="identity.login_succeeded",
            result="succeeded",
            request_id=request_id,
            context={},
        )
        return session_token, csrf_token, identity.user

    @staticmethod
    def digest_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def authenticate(self, token: str) -> AuthenticatedUser:
        stored = await self._store.find_session(self.digest_token(token), datetime.now(UTC))
        if stored is None or stored.revoked_at is not None or stored.user.status != "active":
            raise InvalidCredentials
        return stored.user

    async def verify_csrf(self, token: str, csrf_token: str) -> AuthenticatedUser:
        stored = await self._store.find_session(self.digest_token(token), datetime.now(UTC))
        if (
            stored is None
            or stored.revoked_at is not None
            or not hmac.compare_digest(stored.csrf_digest, self.digest_token(csrf_token))
        ):
            raise InvalidCredentials
        return stored.user

    async def logout(self, token: str, request_id: str) -> None:
        digest = self.digest_token(token)
        stored = await self._store.find_session(digest, datetime.now(UTC))
        if stored is None:
            return
        await self._store.revoke_session(digest, datetime.now(UTC))
        await self._store.append_audit(
            actor_id=stored.user.id,
            action="identity.logout_succeeded",
            result="succeeded",
            request_id=request_id,
            context={},
        )
