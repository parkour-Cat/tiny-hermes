import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from uuid import UUID

from tiny_hermes.identity.domain.models import (
    API_KEY_SCOPES,
    MACHINE_ROLES,
    TOKEN_PREFIX,
    TOKEN_PREFIX_LENGTH,
    ApiKey,
    AuthenticatedMachine,
    IssuedApiKey,
    ServiceAccount,
    ServiceAccountStatus,
    scopes_for_role,
)
from tiny_hermes.identity.ports.machine_store import DuplicateAccountName, MachineIdentityStore
from tiny_hermes.tenancy.domain.models import Actor, Role


class ForbiddenMachineAction(Exception):
    pass


class UnknownServiceAccount(Exception):
    pass


class UnknownApiKey(Exception):
    pass


class InvalidApiKeyScopes(Exception):
    pass


class InvalidServiceAccountRole(Exception):
    pass


class InvalidServiceAccountName(Exception):
    pass


class ServiceAccountNameTaken(Exception):
    pass


class InvalidApiKey(Exception):
    """Presented token is missing, malformed, revoked, expired, or disabled."""


class WorkspaceBindingMismatch(Exception):
    """The caller named a workspace that is not the account's binding."""


class MachineIdentityService:
    def __init__(self, store: MachineIdentityStore) -> None:
        self._store = store

    async def create_account(
        self,
        actor: Actor,
        workspace_id: UUID,
        name: str,
        role: Role,
        request_id: str,
    ) -> ServiceAccount:
        await self._require_admin(actor, workspace_id, request_id)
        if role not in MACHINE_ROLES:
            raise InvalidServiceAccountRole
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise InvalidServiceAccountName
        try:
            account = await self._store.create_account(
                workspace_id, normalized, role, actor.id
            )
        except DuplicateAccountName as error:
            raise ServiceAccountNameTaken from error
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="service_account.created",
            resource_type="service_account",
            resource_id=account.id,
            request_id=request_id,
            context={"role": role.value},
        )
        return account

    async def list_accounts(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> list[ServiceAccount]:
        await self._require_lister(actor, workspace_id, request_id)
        return await self._store.list_accounts(workspace_id)

    async def disable_account(
        self,
        actor: Actor,
        workspace_id: UUID,
        account_id: UUID,
        request_id: str,
    ) -> ServiceAccount:
        await self._require_admin(actor, workspace_id, request_id)
        account = await self._store.disable_account(workspace_id, account_id)
        if account is None:
            raise UnknownServiceAccount
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="service_account.disabled",
            resource_type="service_account",
            resource_id=account.id,
            request_id=request_id,
        )
        return account

    async def create_key(
        self,
        actor: Actor,
        workspace_id: UUID,
        account_id: UUID,
        scopes: tuple[str, ...],
        request_id: str,
        agent_ids: tuple[UUID, ...] = (),
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        await self._require_admin(actor, workspace_id, request_id)
        account = await self._store.get_account(workspace_id, account_id)
        if account is None:
            raise UnknownServiceAccount
        permitted = scopes_for_role(account.role)
        unique = tuple(dict.fromkeys(scopes))
        if not unique or any(scope not in API_KEY_SCOPES for scope in unique):
            raise InvalidApiKeyScopes
        if any(scope not in permitted for scope in unique):
            raise InvalidApiKeyScopes
        token, prefix, digest = _mint_token()
        key = await self._store.create_key(
            account.id, digest, prefix, unique, agent_ids, expires_at
        )
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="api_key.created",
            resource_type="api_key",
            resource_id=key.id,
            request_id=request_id,
            context={"service_account_id": str(account.id), "prefix": prefix},
        )
        return IssuedApiKey(key=key, token=token)

    async def list_keys(
        self,
        actor: Actor,
        workspace_id: UUID,
        account_id: UUID,
        request_id: str,
    ) -> list[ApiKey]:
        await self._require_lister(actor, workspace_id, request_id)
        account = await self._store.get_account(workspace_id, account_id)
        if account is None:
            raise UnknownServiceAccount
        return await self._store.list_keys(account.id)

    async def revoke_key(
        self,
        actor: Actor,
        workspace_id: UUID,
        key_id: UUID,
        request_id: str,
    ) -> ApiKey:
        await self._require_admin(actor, workspace_id, request_id)
        found = await self._store.get_key(key_id)
        if found is None or found[1].workspace_id != workspace_id:
            raise UnknownApiKey
        revoked = await self._store.revoke_key(key_id, datetime.now(UTC))
        if revoked is None:
            raise UnknownApiKey
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="api_key.revoked",
            resource_type="api_key",
            resource_id=revoked.id,
            request_id=request_id,
        )
        return revoked

    async def authenticate(
        self, token: str, workspace_header: UUID | None
    ) -> AuthenticatedMachine:
        """Bind a presented token to a ServiceAccount, never to the key id."""
        if not token.startswith(TOKEN_PREFIX) or len(token) < TOKEN_PREFIX_LENGTH:
            raise InvalidApiKey
        prefix = token[:TOKEN_PREFIX_LENGTH]
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(UTC)
        matched: tuple[ApiKey, ServiceAccount] | None = None
        for key, account, stored_digest in await self._store.keys_with_prefix(prefix):
            if not hmac.compare_digest(stored_digest, digest):
                continue
            matched = (key, account)
            break
        if matched is None:
            raise InvalidApiKey
        key, account = matched
        if key.revoked_at is not None:
            raise InvalidApiKey
        if key.expires_at is not None and key.expires_at <= now:
            raise InvalidApiKey
        if account.status is not ServiceAccountStatus.ACTIVE:
            raise InvalidApiKey
        if workspace_header is not None and workspace_header != account.workspace_id:
            raise WorkspaceBindingMismatch
        effective = frozenset(key.scopes) & scopes_for_role(account.role)
        return AuthenticatedMachine(account=account, key=key, scopes=effective)

    async def _require_admin(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        await self._require_role(
            actor,
            workspace_id,
            request_id,
            allowed={Role.WORKSPACE_ADMIN},
            audit_as_platform="service_account.admin_by_platform_admin",
        )

    async def _require_lister(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        await self._require_role(
            actor,
            workspace_id,
            request_id,
            allowed={Role.WORKSPACE_ADMIN, Role.DEVELOPER},
            audit_as_platform="service_account.list_by_platform_admin",
        )

    async def _require_role(
        self,
        actor: Actor,
        workspace_id: UUID,
        request_id: str,
        *,
        allowed: set[Role],
        audit_as_platform: str,
    ) -> None:
        if actor.is_service_account:
            raise ForbiddenMachineAction
        role = await self._store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenMachineAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenMachineAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=audit_as_platform,
            resource_type="workspace",
            resource_id=workspace_id,
            request_id=request_id,
        )


def _mint_token() -> tuple[str, str, str]:
    token = TOKEN_PREFIX + secrets.token_bytes(32).hex()
    prefix = token[:TOKEN_PREFIX_LENGTH]
    digest = hashlib.sha256(token.encode()).hexdigest()
    return token, prefix, digest
