"""The scope table, one operation at a time.

Shaped after `skills/infrastructure/sql_store.py`: every method is atomic, and
a `None` result means the addressed row is not there rather than that something
went wrong.
"""

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.outbound.application.service import ScopeEntryRecord
from tiny_hermes.outbound.infrastructure.tables import OutboundScopeRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlScopeStore:
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

    async def list_entries(
        self, level: str, workspace_id: UUID | None
    ) -> Sequence[ScopeEntryRecord]:
        query = select(OutboundScopeRow).where(OutboundScopeRow.level == level)
        query = query.where(
            OutboundScopeRow.workspace_id.is_(None)
            if workspace_id is None
            else OutboundScopeRow.workspace_id == workspace_id
        )
        rows = (
            await self._session.scalars(query.order_by(OutboundScopeRow.entry))
        ).all()
        return [_record(row) for row in rows]

    async def add_entry(
        self,
        *,
        level: str,
        workspace_id: UUID | None,
        entry: str,
        note: str | None,
        created_by: UUID,
        endpoint_id: UUID | None = None,
    ) -> ScopeEntryRecord:
        """Approving what is already approved answers with the existing row.

        Idempotent rather than a conflict: an administrator adding an entry
        twice meant the same thing both times, and an endpoint re-registered
        under the same host must not fail on its own previous approval.
        """
        existing = await self._session.scalar(
            select(OutboundScopeRow).where(
                OutboundScopeRow.level == level,
                OutboundScopeRow.workspace_id.is_(None)
                if workspace_id is None
                else OutboundScopeRow.workspace_id == workspace_id,
                OutboundScopeRow.entry == entry,
            )
        )
        if existing is not None:
            return _record(existing)
        row = OutboundScopeRow(
            id=uuid4(),
            level=level,
            workspace_id=workspace_id,
            entry=entry,
            note=note,
            created_by=created_by,
            endpoint_id=endpoint_id,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            # Two writers approving the same entry at once. The unique index is
            # the arbiter; the loser reads what the winner wrote.
            await self._session.rollback()
            found = await self._session.scalar(
                select(OutboundScopeRow).where(
                    OutboundScopeRow.level == level,
                    OutboundScopeRow.workspace_id.is_(None)
                    if workspace_id is None
                    else OutboundScopeRow.workspace_id == workspace_id,
                    OutboundScopeRow.entry == entry,
                )
            )
            if found is None:  # pragma: no cover - the index said it exists
                raise
            return _record(found)
        return _record(row)

    async def get_entry(self, entry_id: UUID) -> ScopeEntryRecord | None:
        row = await self._session.get(OutboundScopeRow, entry_id)
        return None if row is None else _record(row)

    async def remove_entry(self, entry_id: UUID) -> ScopeEntryRecord | None:
        row = await self._session.get(OutboundScopeRow, entry_id)
        if row is None:
            return None
        record = _record(row)
        await self._session.delete(row)
        await self._session.flush()
        return record

    async def remove_endpoint_entries(self, endpoint_id: UUID) -> int:
        rows = (
            await self._session.scalars(
                select(OutboundScopeRow).where(
                    OutboundScopeRow.endpoint_id == endpoint_id
                )
            )
        ).all()
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        return len(rows)

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
                id=uuid4(),
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="outbound_scope",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )
        await self._session.flush()


def _record(row: OutboundScopeRow) -> ScopeEntryRecord:
    return ScopeEntryRecord(
        id=row.id,
        level=row.level,
        workspace_id=row.workspace_id,
        entry=row.entry,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
        endpoint_id=row.endpoint_id,
    )
