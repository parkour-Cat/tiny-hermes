from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.identity.domain.models import ApiKey, ServiceAccount
from tiny_hermes.tenancy.domain.models import Role


class DuplicateAccountName(Exception):
    pass


class MachineIdentityStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_account(
        self,
        workspace_id: UUID,
        name: str,
        role: Role,
        created_by_user_id: UUID,
    ) -> ServiceAccount: ...

    async def get_account(
        self, workspace_id: UUID, account_id: UUID
    ) -> ServiceAccount | None: ...

    async def list_accounts(self, workspace_id: UUID) -> list[ServiceAccount]: ...

    async def disable_account(
        self, workspace_id: UUID, account_id: UUID
    ) -> ServiceAccount | None: ...

    async def create_key(
        self,
        service_account_id: UUID,
        token_digest: str,
        prefix: str,
        scopes: tuple[str, ...],
        agent_ids: tuple[UUID, ...],
        expires_at: datetime | None,
    ) -> ApiKey: ...

    async def list_keys(self, service_account_id: UUID) -> list[ApiKey]: ...

    async def get_key(self, key_id: UUID) -> tuple[ApiKey, ServiceAccount] | None: ...

    async def revoke_key(self, key_id: UUID, now: datetime) -> ApiKey | None: ...

    async def keys_with_prefix(self, prefix: str) -> list[tuple[ApiKey, ServiceAccount, str]]:
        """Return matching keys with their digest so the service can compare."""
        ...

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
    ) -> None: ...
