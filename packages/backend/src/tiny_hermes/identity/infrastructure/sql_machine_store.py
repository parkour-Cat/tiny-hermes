from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.domain.models import (
    ApiKey,
    ServiceAccount,
    ServiceAccountStatus,
)
from tiny_hermes.identity.infrastructure.tables import ApiKeyRow, ServiceAccountRow
from tiny_hermes.identity.ports.machine_store import DuplicateAccountName
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlMachineIdentityStore:
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

    async def create_account(
        self,
        workspace_id: UUID,
        name: str,
        role: Role,
        created_by_user_id: UUID,
    ) -> ServiceAccount:
        row = ServiceAccountRow(
            workspace_id=workspace_id,
            name=name,
            role=role.value,
            status=ServiceAccountStatus.ACTIVE.value,
            created_by_user_id=created_by_user_id,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise DuplicateAccountName from error
        return _account(row)

    async def get_account(
        self, workspace_id: UUID, account_id: UUID
    ) -> ServiceAccount | None:
        row = await self._session.scalar(
            select(ServiceAccountRow).where(
                ServiceAccountRow.id == account_id,
                ServiceAccountRow.workspace_id == workspace_id,
            )
        )
        return None if row is None else _account(row)

    async def list_accounts(self, workspace_id: UUID) -> list[ServiceAccount]:
        rows = (
            await self._session.scalars(
                select(ServiceAccountRow)
                .where(ServiceAccountRow.workspace_id == workspace_id)
                .order_by(ServiceAccountRow.created_at, ServiceAccountRow.id)
            )
        ).all()
        return [_account(row) for row in rows]

    async def disable_account(
        self, workspace_id: UUID, account_id: UUID
    ) -> ServiceAccount | None:
        row = await self._session.scalar(
            select(ServiceAccountRow).where(
                ServiceAccountRow.id == account_id,
                ServiceAccountRow.workspace_id == workspace_id,
            )
        )
        if row is None:
            return None
        row.status = ServiceAccountStatus.DISABLED.value
        await self._session.flush()
        return _account(row)

    async def create_key(
        self,
        service_account_id: UUID,
        token_digest: str,
        prefix: str,
        scopes: tuple[str, ...],
        agent_ids: tuple[UUID, ...],
        expires_at: datetime | None,
    ) -> ApiKey:
        row = ApiKeyRow(
            service_account_id=service_account_id,
            token_digest=token_digest,
            prefix=prefix,
            scopes=list(scopes),
            agent_ids=[str(item) for item in agent_ids],
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _key(row)

    async def list_keys(self, service_account_id: UUID) -> list[ApiKey]:
        rows = (
            await self._session.scalars(
                select(ApiKeyRow)
                .where(ApiKeyRow.service_account_id == service_account_id)
                .order_by(ApiKeyRow.created_at, ApiKeyRow.id)
            )
        ).all()
        return [_key(row) for row in rows]

    async def get_key(self, key_id: UUID) -> tuple[ApiKey, ServiceAccount] | None:
        result = await self._session.execute(
            select(ApiKeyRow, ServiceAccountRow)
            .join(
                ServiceAccountRow,
                ServiceAccountRow.id == ApiKeyRow.service_account_id,
            )
            .where(ApiKeyRow.id == key_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        key, account = row
        return _key(key), _account(account)

    async def revoke_key(self, key_id: UUID, now: datetime) -> ApiKey | None:
        row = await self._session.get(ApiKeyRow, key_id)
        if row is None:
            return None
        if row.revoked_at is None:
            row.revoked_at = now
            await self._session.flush()
        return _key(row)

    async def keys_with_prefix(
        self, prefix: str
    ) -> list[tuple[ApiKey, ServiceAccount, str]]:
        rows = (
            await self._session.execute(
                select(ApiKeyRow, ServiceAccountRow)
                .join(
                    ServiceAccountRow,
                    ServiceAccountRow.id == ApiKeyRow.service_account_id,
                )
                .where(ApiKeyRow.prefix == prefix)
            )
        ).all()
        return [(_key(key), _account(account), key.token_digest) for key, account in rows]

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
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )


def _account(row: ServiceAccountRow) -> ServiceAccount:
    return ServiceAccount(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        role=Role(row.role),
        status=ServiceAccountStatus(row.status),
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _key(row: ApiKeyRow) -> ApiKey:
    raw_ids: list[Any] = row.agent_ids or []
    return ApiKey(
        id=row.id,
        service_account_id=row.service_account_id,
        prefix=row.prefix,
        scopes=tuple(str(item) for item in row.scopes or ()),
        agent_ids=tuple(UUID(str(item)) for item in raw_ids),
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )
