"""In-memory `AuditStore`, for the unit suite — the same role every other
`memory_store.py` in this codebase plays: a stand-in for `SqlAuditStore`'s
behaviour, not a shortcut to a different one. The own-resources filtering
and the "fetch one past the limit" pagination trick are implemented here in
plain Python so `AuditService`'s unit tests do not need a database, but the
*rule* they implement is `AuditService`'s own docstring, not this module's —
duplicated in `SqlAuditStore` as SQL rather than trusted to match by
accident.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from tiny_hermes.audit.domain.query import AuditFilter, AuditPage
from tiny_hermes.audit.domain.record import AuditRecord
from tiny_hermes.audit.domain.scope import AuditScope, AuditVisibility
from tiny_hermes.tenancy.domain.models import Role


class MemoryAuditStore:
    def __init__(self) -> None:
        self._rows: list[AuditRecord] = []
        self._roles: dict[tuple[UUID, UUID], Role] = {}

    def seed(self, record: AuditRecord) -> AuditRecord:
        self._rows.append(record)
        return record

    def set_role(self, workspace_id: UUID, user_id: UUID, role: Role) -> None:
        self._roles[(workspace_id, user_id)] = role

    def rows_with_action(self, action: str) -> list[AuditRecord]:
        return [row for row in self._rows if row.action == action]

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self._roles.get((workspace_id, user_id))

    async def query(self, scope: AuditScope, filters: AuditFilter) -> AuditPage:
        candidates = [row for row in self._rows if row.workspace_id == scope.workspace_id]
        if scope.visibility is AuditVisibility.OWN_RESOURCES:
            # `AuditScope.own_resources` cannot be built without an
            # `actor_id` (its own `__post_init__`), so equality against it
            # below is well-defined without narrowing the `UUID | None`
            # further.
            touched = {
                row.resource_id
                for row in candidates
                if row.actor_id == scope.actor_id and row.resource_id is not None
            }
            candidates = [
                row
                for row in candidates
                if row.actor_id == scope.actor_id or row.resource_id in touched
            ]
        if filters.action is not None:
            candidates = [row for row in candidates if row.action == filters.action]
        if filters.resource_type is not None:
            candidates = [
                row for row in candidates if row.resource_type == filters.resource_type
            ]
        if filters.actor_id is not None:
            candidates = [row for row in candidates if row.actor_id == filters.actor_id]
        if filters.since is not None:
            since = filters.since
            candidates = [row for row in candidates if row.created_at >= since]
        if filters.until is not None:
            until = filters.until
            candidates = [row for row in candidates if row.created_at <= until]
        candidates.sort(key=lambda row: (row.created_at, str(row.id)), reverse=True)
        window = candidates[filters.offset : filters.offset + filters.limit + 1]
        has_more = len(window) > filters.limit
        return AuditPage(items=tuple(window[: filters.limit]), has_more=has_more)

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
        context: dict[str, Any] | None = None,
    ) -> None:
        self._rows.append(
            AuditRecord(
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
