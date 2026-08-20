"""`EndUserStore`, backed by the three design-§3 tables plus
`end_user_sessions`.

Exercised through the API integration tests
(`tests/integration/identity/test_end_user_sessions.py`) rather than in
isolation — the same choice `SqlMachineIdentityStore` and `SqlAuthStore` make
elsewhere in this package. A query against a real Postgres is the only thing
that proves the composite foreign keys and the `UNIQUE` constraints this
store leans on actually hold.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.domain.models import ChannelIssuerStatus
from tiny_hermes.identity.infrastructure.end_user_session_tables import EndUserSessionRow
from tiny_hermes.identity.infrastructure.end_user_tables import (
    ChannelIssuerRow,
    EndUserRow,
    ExternalIdentityRow,
)
from tiny_hermes.identity.ports.end_user_store import (
    ChannelIssuerRecord,
    IssuerAlreadyRegistered,
    StoredEndUserSession,
    UpsertedIdentity,
)
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlEndUserStore:
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

    # -- channel_issuers, design §3 ---------------------------------------

    async def create_issuer(
        self,
        *,
        workspace_id: UUID,
        channel: str,
        issuer: str,
        public_key: str | None,
        jwks_url: str | None,
        allowed_origins: Sequence[str],
        created_by: UUID,
    ) -> ChannelIssuerRecord:
        row = ChannelIssuerRow(
            workspace_id=workspace_id,
            channel=channel,
            issuer=issuer,
            public_key=public_key,
            jwks_url=jwks_url,
            allowed_origins=list(allowed_origins),
            status=ChannelIssuerStatus.ACTIVE.value,
            created_by=created_by,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise IssuerAlreadyRegistered from error
        return _issuer_record(row)

    async def list_issuers(self, workspace_id: UUID) -> Sequence[ChannelIssuerRecord]:
        result = await self._session.scalars(
            select(ChannelIssuerRow)
            .where(ChannelIssuerRow.workspace_id == workspace_id)
            .order_by(ChannelIssuerRow.created_at)
        )
        return [_issuer_record(row) for row in result]

    async def disable_issuer(
        self, workspace_id: UUID, issuer_id: UUID
    ) -> ChannelIssuerRecord | None:
        row = await self._session.scalar(
            select(ChannelIssuerRow).where(
                ChannelIssuerRow.id == issuer_id, ChannelIssuerRow.workspace_id == workspace_id
            )
        )
        if row is None:
            return None
        row.status = ChannelIssuerStatus.DISABLED.value
        await self._session.flush()
        return _issuer_record(row)

    async def find_issuer(self, workspace_id: UUID, issuer: str) -> ChannelIssuerRecord | None:
        result = await self._session.scalars(
            select(ChannelIssuerRow).where(
                ChannelIssuerRow.workspace_id == workspace_id,
                ChannelIssuerRow.issuer == issuer,
            )
        )
        rows = result.all()
        # An issuer string registered under two channels for the same
        # workspace is ambiguous at exchange time (see the port's
        # docstring): treated as no match, which routes it into the same
        # fixed-cost refusal path as an issuer nobody registered at all.
        return _issuer_record(rows[0]) if len(rows) == 1 else None

    # -- external_identities, design §282 ---------------------------------

    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedIdentity:
        existing = await self._session.execute(
            select(ExternalIdentityRow.end_user_id, EndUserRow.erased_at)
            .join(EndUserRow, EndUserRow.id == ExternalIdentityRow.end_user_id)
            .where(
                ExternalIdentityRow.workspace_id == workspace_id,
                ExternalIdentityRow.channel == channel,
                ExternalIdentityRow.external_user_id == external_user_id,
            )
        )
        row = existing.one_or_none()
        if row is not None:
            return UpsertedIdentity(end_user_id=row[0], erased_at=row[1])

        end_user = EndUserRow(workspace_id=workspace_id)
        self._session.add(end_user)
        await self._session.flush()
        self._session.add(
            ExternalIdentityRow(
                workspace_id=workspace_id,
                channel=channel,
                external_user_id=external_user_id,
                end_user_id=end_user.id,
            )
        )
        await self._session.flush()
        return UpsertedIdentity(end_user_id=end_user.id, erased_at=None)

    async def end_user_exists(self, workspace_id: UUID, end_user_id: UUID) -> bool:
        value = await self._session.scalar(
            select(EndUserRow.id).where(
                EndUserRow.id == end_user_id, EndUserRow.workspace_id == workspace_id
            )
        )
        return value is not None

    # -- end_user_sessions, design §4.2-4.3 --------------------------------

    async def create_session(
        self, end_user_id: UUID, workspace_id: UUID, token_digest: str, expires_at: datetime
    ) -> None:
        self._session.add(
            EndUserSessionRow(
                end_user_id=end_user_id,
                workspace_id=workspace_id,
                token_digest=token_digest,
                expires_at=expires_at,
                revoked_at=None,
            )
        )

    async def find_session(
        self, token_digest: str, now: datetime
    ) -> StoredEndUserSession | None:
        row = await self._session.scalar(
            select(EndUserSessionRow).where(
                EndUserSessionRow.token_digest == token_digest,
                EndUserSessionRow.expires_at > now,
            )
        )
        if row is None or row.revoked_at is not None:
            return None
        return StoredEndUserSession(row.end_user_id, row.workspace_id)

    async def revoke_sessions(self, end_user_id: UUID, workspace_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(EndUserSessionRow)
            .where(
                EndUserSessionRow.end_user_id == end_user_id,
                EndUserSessionRow.workspace_id == workspace_id,
                EndUserSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_type: str,
        actor_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        request_id: str,
        context: dict[str, str],
    ) -> None:
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context,
            )
        )


def _issuer_record(row: ChannelIssuerRow) -> ChannelIssuerRecord:
    return ChannelIssuerRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        channel=row.channel,
        issuer=row.issuer,
        public_key=row.public_key,
        jwks_url=row.jwks_url,
        allowed_origins=tuple(row.allowed_origins),
        status=ChannelIssuerStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
    )
