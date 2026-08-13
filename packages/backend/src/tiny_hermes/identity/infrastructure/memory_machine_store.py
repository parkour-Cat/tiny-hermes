from datetime import UTC, datetime
from uuid import UUID, uuid4

from tiny_hermes.identity.domain.models import (
    ApiKey,
    ServiceAccount,
    ServiceAccountStatus,
)
from tiny_hermes.identity.ports.machine_store import DuplicateAccountName
from tiny_hermes.tenancy.domain.models import Role


class MemoryMachineIdentityStore:
    def __init__(self) -> None:
        self.accounts: dict[UUID, ServiceAccount] = {}
        self.keys: dict[UUID, tuple[ApiKey, str]] = {}
        self.memberships: dict[tuple[UUID, UUID], Role] = {}
        self.audit_actions: list[str] = []

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.memberships.get((workspace_id, user_id))

    async def create_account(
        self,
        workspace_id: UUID,
        name: str,
        role: Role,
        created_by_user_id: UUID,
    ) -> ServiceAccount:
        for account in self.accounts.values():
            if account.workspace_id == workspace_id and account.name == name:
                raise DuplicateAccountName
        row = ServiceAccount(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            role=role,
            status=ServiceAccountStatus.ACTIVE,
            created_by_user_id=created_by_user_id,
            created_at=datetime.now(UTC),
        )
        self.accounts[row.id] = row
        return row

    async def get_account(
        self, workspace_id: UUID, account_id: UUID
    ) -> ServiceAccount | None:
        account = self.accounts.get(account_id)
        if account is None or account.workspace_id != workspace_id:
            return None
        return account

    async def list_accounts(self, workspace_id: UUID) -> list[ServiceAccount]:
        return [
            account
            for account in self.accounts.values()
            if account.workspace_id == workspace_id
        ]

    async def disable_account(
        self, workspace_id: UUID, account_id: UUID
    ) -> ServiceAccount | None:
        account = await self.get_account(workspace_id, account_id)
        if account is None:
            return None
        disabled = ServiceAccount(
            id=account.id,
            workspace_id=account.workspace_id,
            name=account.name,
            role=account.role,
            status=ServiceAccountStatus.DISABLED,
            created_by_user_id=account.created_by_user_id,
            created_at=account.created_at,
        )
        self.accounts[account.id] = disabled
        return disabled

    async def create_key(
        self,
        service_account_id: UUID,
        token_digest: str,
        prefix: str,
        scopes: tuple[str, ...],
        agent_ids: tuple[UUID, ...],
        expires_at: datetime | None,
    ) -> ApiKey:
        key = ApiKey(
            id=uuid4(),
            service_account_id=service_account_id,
            prefix=prefix,
            scopes=scopes,
            agent_ids=agent_ids,
            expires_at=expires_at,
            revoked_at=None,
            created_at=datetime.now(UTC),
        )
        self.keys[key.id] = (key, token_digest)
        return key

    async def list_keys(self, service_account_id: UUID) -> list[ApiKey]:
        return [
            key
            for key, _ in self.keys.values()
            if key.service_account_id == service_account_id
        ]

    async def get_key(self, key_id: UUID) -> tuple[ApiKey, ServiceAccount] | None:
        stored = self.keys.get(key_id)
        if stored is None:
            return None
        key, _ = stored
        account = self.accounts.get(key.service_account_id)
        if account is None:
            return None
        return key, account

    async def revoke_key(self, key_id: UUID, now: datetime) -> ApiKey | None:
        stored = self.keys.get(key_id)
        if stored is None:
            return None
        key, digest = stored
        revoked = ApiKey(
            id=key.id,
            service_account_id=key.service_account_id,
            prefix=key.prefix,
            scopes=key.scopes,
            agent_ids=key.agent_ids,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at or now,
            created_at=key.created_at,
        )
        self.keys[key_id] = (revoked, digest)
        return revoked

    async def keys_with_prefix(
        self, prefix: str
    ) -> list[tuple[ApiKey, ServiceAccount, str]]:
        found: list[tuple[ApiKey, ServiceAccount, str]] = []
        for key, digest in self.keys.values():
            if key.prefix != prefix:
                continue
            account = self.accounts.get(key.service_account_id)
            if account is None:
                continue
            found.append((key, account, digest))
        return found

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_type: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        del workspace_id, actor_id, resource_type, resource_id, request_id, context
        self.audit_actions.append(action)
