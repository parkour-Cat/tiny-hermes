from datetime import UTC, datetime
from uuid import UUID

from tiny_hermes.secrets.domain.models import SecretRecord, SecretScope, SecretStatus
from tiny_hermes.secrets.ports.store import DuplicateSecretName
from tiny_hermes.tenancy.domain.models import Role


class MemorySecretStore:
    def __init__(self) -> None:
        self.records: dict[UUID, SecretRecord] = {}
        self.memberships: dict[tuple[UUID, UUID], Role] = {}
        self.audit_actions: list[str] = []

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.memberships.get((workspace_id, user_id))

    async def create(self, record: SecretRecord) -> SecretRecord:
        self._reject_duplicate(record)
        self.records[record.id] = record
        return record

    async def get(self, secret_id: UUID) -> SecretRecord | None:
        return self.records.get(secret_id)

    async def list_visible(self, workspace_id: UUID) -> list[SecretRecord]:
        visible = [
            record
            for record in self.records.values()
            if record.scope is SecretScope.PLATFORM
            or record.workspace_id == workspace_id
        ]
        return sorted(visible, key=lambda item: (item.created_at, item.id))

    async def disable(self, secret_id: UUID) -> SecretRecord | None:
        record = self.records.get(secret_id)
        if record is None:
            return None
        disabled = SecretRecord(
            id=record.id,
            name=record.name,
            scope=record.scope,
            workspace_id=record.workspace_id,
            status=SecretStatus.DISABLED,
            mask=record.mask,
            ciphertext=record.ciphertext,
            nonce=record.nonce,
            wrapped_dek=record.wrapped_dek,
            wrap_nonce=record.wrap_nonce,
            key_id=record.key_id,
            created_at=record.created_at,
            updated_at=datetime.now(UTC),
        )
        self.records[secret_id] = disabled
        return disabled

    async def list_by_key_id(self, key_id: str) -> list[SecretRecord]:
        return [record for record in self.records.values() if record.key_id == key_id]

    async def replace_wrap(
        self,
        secret_id: UUID,
        wrapped_dek: bytes,
        wrap_nonce: bytes,
        key_id: str,
    ) -> SecretRecord | None:
        record = self.records.get(secret_id)
        if record is None:
            return None
        updated = SecretRecord(
            id=record.id,
            name=record.name,
            scope=record.scope,
            workspace_id=record.workspace_id,
            status=record.status,
            mask=record.mask,
            ciphertext=record.ciphertext,
            nonce=record.nonce,
            wrapped_dek=wrapped_dek,
            wrap_nonce=wrap_nonce,
            key_id=key_id,
            created_at=record.created_at,
            updated_at=datetime.now(UTC),
        )
        self.records[secret_id] = updated
        return updated

    async def count_by_key_id(self, key_id: str) -> int:
        return sum(1 for record in self.records.values() if record.key_id == key_id)

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        del workspace_id, actor_id, resource_id, request_id, context
        self.audit_actions.append(action)

    def _reject_duplicate(self, record: SecretRecord) -> None:
        for existing in self.records.values():
            if existing.scope is not record.scope:
                continue
            if existing.name != record.name:
                continue
            if existing.workspace_id != record.workspace_id:
                continue
            raise DuplicateSecretName
