from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.domain.models import OidcProviderStatus
from tiny_hermes.identity.infrastructure.tables import OidcLoginStateRow, OidcProviderRow
from tiny_hermes.identity.ports.oidc_store import OidcLoginStateRecord, OidcProviderRecord


class SqlOidcProviderStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_provider(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret_ref: str,
        discovery_url: str,
        scopes: Sequence[str],
        created_by: UUID,
    ) -> OidcProviderRecord:
        row = OidcProviderRow(
            issuer=issuer,
            client_id=client_id,
            client_secret_ref=client_secret_ref,
            discovery_url=discovery_url,
            scopes=list(scopes),
            status=OidcProviderStatus.ACTIVE.value,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return _record(row)

    async def get_provider(self, provider_id: UUID) -> OidcProviderRecord | None:
        row = await self._session.get(OidcProviderRow, provider_id)
        return None if row is None else _record(row)

    async def list_providers(self) -> Sequence[OidcProviderRecord]:
        result = await self._session.scalars(
            select(OidcProviderRow).order_by(OidcProviderRow.created_at, OidcProviderRow.id)
        )
        return [_record(row) for row in result.all()]

    async def disable_provider(self, provider_id: UUID) -> OidcProviderRecord | None:
        row = await self._session.get(OidcProviderRow, provider_id)
        if row is None:
            return None
        row.status = OidcProviderStatus.DISABLED.value
        await self._session.flush()
        return _record(row)

    async def create_login_state(
        self,
        *,
        provider_id: UUID,
        state: str,
        nonce: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            OidcLoginStateRow(
                provider_id=provider_id,
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
                expires_at=expires_at,
                consumed_at=None,
            )
        )

    async def consume_login_state(
        self, state: str, provider_id: UUID, now: datetime
    ) -> OidcLoginStateRecord | None:
        # A single `UPDATE ... WHERE consumed_at IS NULL ... RETURNING` rather
        # than a read-then-write: two concurrent callbacks presenting the same
        # `state` both reach this at once, and the database's own row lock is
        # what decides which one wins. A read-then-write here would let both
        # read "not yet consumed" before either writes, and a replayed `state`
        # would mint two sessions instead of one refusal.
        result = await self._session.execute(
            update(OidcLoginStateRow)
            .where(
                OidcLoginStateRow.state == state,
                OidcLoginStateRow.provider_id == provider_id,
                OidcLoginStateRow.consumed_at.is_(None),
                OidcLoginStateRow.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(OidcLoginStateRow)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return OidcLoginStateRecord(
            provider_id=row.provider_id,
            nonce=row.nonce,
            code_verifier=row.code_verifier,
            redirect_uri=row.redirect_uri,
        )

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


def _record(row: OidcProviderRow) -> OidcProviderRecord:
    return OidcProviderRecord(
        id=row.id,
        issuer=row.issuer,
        client_id=row.client_id,
        client_secret_ref=row.client_secret_ref,
        discovery_url=row.discovery_url,
        scopes=tuple(row.scopes),
        status=OidcProviderStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
    )
