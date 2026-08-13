from typing import Protocol
from uuid import UUID

from tiny_hermes.secrets.domain.models import SecretRecord
from tiny_hermes.tenancy.domain.models import Role


class DuplicateSecretName(Exception):
    pass


class SecretStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create(self, record: SecretRecord) -> SecretRecord: ...

    async def get(self, secret_id: UUID) -> SecretRecord | None: ...

    async def list_visible(self, workspace_id: UUID) -> list[SecretRecord]:
        """Workspace secrets for this workspace, plus every platform secret."""
        ...

    async def disable(self, secret_id: UUID) -> SecretRecord | None: ...

    async def list_by_key_id(self, key_id: str) -> list[SecretRecord]: ...

    async def replace_wrap(
        self,
        secret_id: UUID,
        wrapped_dek: bytes,
        wrap_nonce: bytes,
        key_id: str,
    ) -> SecretRecord | None: ...

    async def count_by_key_id(self, key_id: str) -> int: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...
