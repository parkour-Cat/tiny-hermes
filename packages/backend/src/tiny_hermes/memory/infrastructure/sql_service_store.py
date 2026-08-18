"""Reading and deciding memory rows for the review service.

Separate from `SqlMemoryLibrary`, which is the Run's read path and takes a
scope so it can never ask for more than one. This store answers the console's
questions instead — "what is waiting in this workspace", "this one by id" —
which are wider by design and gated by the service's role check rather than by
the shape of a query.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.agents.infrastructure.tables import AgentRow
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.memory.application.service import MemoryRecord
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryStatus
from tiny_hermes.memory.infrastructure.tables import MemoryRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlMemoryStore:
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

    async def list_pending(self, workspace_id: UUID) -> Sequence[MemoryRecord]:
        rows = (
            await self._session.scalars(
                select(MemoryRow)
                .where(
                    MemoryRow.workspace_id == workspace_id,
                    MemoryRow.status == MemoryStatus.PENDING.value,
                )
                .order_by(MemoryRow.created_at)
            )
        ).all()
        return [_record(row) for row in rows]

    async def get(self, memory_id: UUID) -> MemoryRecord | None:
        row = await self._session.get(MemoryRow, memory_id)
        return None if row is None else _record(row)

    async def set_status(
        self, memory_id: UUID, status: MemoryStatus, now: datetime
    ) -> MemoryRecord | None:
        row = await self._session.get(MemoryRow, memory_id)
        if row is None:  # pragma: no cover - the service read it first
            return None
        row.status = status.value
        row.updated_at = now
        await self._session.flush()
        return _record(row)

    async def create_shared(
        self, *, workspace_id: UUID, agent_id: UUID, body: str, created_by: UUID
    ) -> MemoryRecord:
        now = datetime.now(UTC)
        row = MemoryRow(
            id=uuid4(),
            workspace_id=workspace_id,
            agent_id=agent_id,
            kind=MemoryKind.SHARED.value,
            subject_type=None,
            subject_id=None,
            body=body,
            status=MemoryStatus.ACTIVE.value,
            origin="operator",
            origin_run_id=None,
            context={},
            created_by=created_by,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _record(row)

    async def agent_in_workspace(self, workspace_id: UUID, agent_id: UUID) -> bool:
        found = await self._session.scalar(
            select(AgentRow.id).where(
                AgentRow.id == agent_id, AgentRow.workspace_id == workspace_id
            )
        )
        return found is not None

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
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
                resource_type="memory",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )
        await self._session.flush()


def _record(row: MemoryRow) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        kind=MemoryKind(row.kind),
        status=MemoryStatus(row.status),
        body=row.body,
        origin=row.origin,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
