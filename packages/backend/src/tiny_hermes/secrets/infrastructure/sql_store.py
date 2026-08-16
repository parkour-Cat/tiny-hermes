from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.secrets.domain.models import SecretRecord, SecretScope, SecretStatus
from tiny_hermes.secrets.infrastructure.tables import SecretRow
from tiny_hermes.secrets.ports.store import DuplicateSecretName
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlSecretStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def create(self, record: SecretRecord) -> SecretRecord:
        row = SecretRow(
            id=record.id,
            name=record.name,
            scope=record.scope.value,
            workspace_id=record.workspace_id,
            status=record.status.value,
            mask=record.mask,
            ciphertext=record.ciphertext,
            nonce=record.nonce,
            wrapped_dek=record.wrapped_dek,
            wrap_nonce=record.wrap_nonce,
            key_id=record.key_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise DuplicateSecretName from error
        return _record(row)

    async def get(self, secret_id: UUID) -> SecretRecord | None:
        row = await self._session.get(SecretRow, secret_id)
        return None if row is None else _record(row)

    async def list_visible(self, workspace_id: UUID) -> list[SecretRecord]:
        rows = (
            await self._session.scalars(
                select(SecretRow)
                .where(
                    (SecretRow.scope == SecretScope.PLATFORM.value)
                    | (SecretRow.workspace_id == workspace_id)
                )
                .order_by(SecretRow.created_at, SecretRow.id)
            )
        ).all()
        return [_record(row) for row in rows]

    async def disable(self, secret_id: UUID) -> SecretRecord | None:
        row = await self._session.get(SecretRow, secret_id)
        if row is None:
            return None
        row.status = SecretStatus.DISABLED.value
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _record(row)

    async def list_by_key_id(self, key_id: str) -> list[SecretRecord]:
        rows = (
            await self._session.scalars(
                select(SecretRow)
                .where(SecretRow.key_id == key_id)
                .order_by(SecretRow.created_at, SecretRow.id)
            )
        ).all()
        return [_record(row) for row in rows]

    async def replace_wrap(
        self,
        secret_id: UUID,
        wrapped_dek: bytes,
        wrap_nonce: bytes,
        key_id: str,
    ) -> SecretRecord | None:
        row = await self._session.get(SecretRow, secret_id)
        if row is None:
            return None
        row.wrapped_dek = wrapped_dek
        row.wrap_nonce = wrap_nonce
        row.key_id = key_id
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _record(row)

    async def count_by_key_id(self, key_id: str) -> int:
        value = await self._session.scalar(
            select(func.count()).select_from(SecretRow).where(SecretRow.key_id == key_id)
        )
        return int(value or 0)

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
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="secret",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )


def _record(row: SecretRow) -> SecretRecord:
    return SecretRecord(
        id=row.id,
        name=row.name,
        scope=SecretScope(row.scope),
        workspace_id=row.workspace_id,
        status=SecretStatus(row.status),
        mask=row.mask,
        ciphertext=bytes(row.ciphertext),
        nonce=bytes(row.nonce),
        wrapped_dek=bytes(row.wrapped_dek),
        wrap_nonce=bytes(row.wrap_nonce),
        key_id=row.key_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
