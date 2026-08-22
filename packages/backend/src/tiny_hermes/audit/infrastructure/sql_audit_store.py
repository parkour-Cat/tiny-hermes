"""§1's real store: paginated, newest-first, filterable by `action` /
`resource_type` / `actor_id` / a time window.

The own-resources query (§4.6's 开发者 row) is the one part of this file
worth reading closely. `AuditService`'s own docstring gives the definition
this SQL implements: a developer's own actions, plus every row whose
`resource_id` also names a resource this developer has acted on before —
computed with a correlated subquery against `audit_events` itself rather
than a join to any other table, because no other table in this schema
records who owns a resource. `MemoryAuditStore` implements the identical
rule in Python for the unit suite; this is the same rule as SQL, not a
different one that happens to agree on the cases anybody thought to test.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.domain.query import AuditFilter, AuditPage
from tiny_hermes.audit.domain.record import AuditRecord
from tiny_hermes.audit.domain.scope import AuditScope, AuditVisibility
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlAuditStore:
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

    async def query(self, scope: AuditScope, filters: AuditFilter) -> AuditPage:
        query = select(AuditEventRow).where(AuditEventRow.workspace_id == scope.workspace_id)
        if scope.visibility is AuditVisibility.OWN_RESOURCES:
            touched = select(AuditEventRow.resource_id).where(
                AuditEventRow.workspace_id == scope.workspace_id,
                AuditEventRow.actor_id == scope.actor_id,
                AuditEventRow.resource_id.is_not(None),
            )
            query = query.where(
                or_(
                    AuditEventRow.actor_id == scope.actor_id,
                    AuditEventRow.resource_id.in_(touched),
                )
            )
        if filters.action is not None:
            query = query.where(AuditEventRow.action == filters.action)
        if filters.resource_type is not None:
            query = query.where(AuditEventRow.resource_type == filters.resource_type)
        if filters.actor_id is not None:
            query = query.where(AuditEventRow.actor_id == filters.actor_id)
        if filters.since is not None:
            query = query.where(AuditEventRow.created_at >= filters.since)
        if filters.until is not None:
            query = query.where(AuditEventRow.created_at <= filters.until)
        query = query.order_by(AuditEventRow.created_at.desc(), AuditEventRow.id.desc())
        # Fetch one row past the limit to learn `has_more` without a second
        # `COUNT(*)` round-trip — `memory/domain/search.py`'s neighbourhood
        # of this codebase does not use this trick, but `AuditPage`'s own
        # docstring names it as the contract every `AuditStore` honours.
        rows = (
            await self._session.scalars(
                query.offset(filters.offset).limit(filters.limit + 1)
            )
        ).all()
        has_more = len(rows) > filters.limit
        items = tuple(_record(row) for row in rows[: filters.limit])
        return AuditPage(items=items, has_more=has_more)

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        actor_type: str,
        action: str,
        resource_type: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        self._session.add(
            AuditEventRow(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
                created_at=datetime.now(UTC),
            )
        )
        await self._session.flush()


def _record(row: AuditEventRow) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        result=row.result,
        request_id=row.request_id,
        context=dict(row.context or {}),
        created_at=row.created_at,
    )
