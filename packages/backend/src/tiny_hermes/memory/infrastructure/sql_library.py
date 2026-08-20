"""Reading one scope's memories, and nothing wider.

Every query in this module filters on all four scope columns and on `active`.
There is no method that takes fewer, and adding one would be the change that
makes §14.1's isolation depend on a caller remembering to narrow — which is
exactly the shape the port exists to prevent.

`subject_id IS NULL` for a shared scope rather than "any subject": a null
comparison in SQL is not a wildcard, and writing it as one would turn the
shared read into a read of everybody's.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.memory.domain.scope import MemoryKind, MemoryScope, MemoryStatus
from tiny_hermes.memory.infrastructure.tables import SEARCH_CONFIG, MemoryRow
from tiny_hermes.memory.ports.library import RememberedFact
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType


class SqlMemoryLibrary:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_in(
        self, scope: MemoryScope, *, limit: int
    ) -> Sequence[RememberedFact]:
        rows = (
            await self._session.scalars(
                _scoped(scope)
                .where(MemoryRow.status == MemoryStatus.ACTIVE.value)
                .order_by(MemoryRow.created_at.desc())
                .limit(limit)
            )
        ).all()
        return [_fact(row, scope) for row in rows]

    async def relevant_in(
        self, scope: MemoryScope, query: str, *, limit: int
    ) -> Sequence[RememberedFact]:
        """The same scoped read, ordered by keyword relevance to `query`.

        `plainto_tsquery('simple', ...)` turns the Run's input into a query the
        same way the stored `search` column was built — `simple`, so a stemmer
        for one language does not mangle the other. Rank first, recency to break
        ties, and a blank or match-less query still returns the scope's rows by
        recency rather than nothing: a memory that does not match this turn is
        still this subject's, and the segment budget decides what fits.
        """
        cleaned = query.strip()
        if not cleaned:
            return await self.active_in(scope, limit=limit)
        rank = func.ts_rank(
            MemoryRow.search, func.plainto_tsquery(SEARCH_CONFIG, cleaned)
        )
        rows = (
            await self._session.scalars(
                _scoped(scope)
                .where(MemoryRow.status == MemoryStatus.ACTIVE.value)
                .order_by(rank.desc(), MemoryRow.created_at.desc())
                .limit(limit)
            )
        ).all()
        return [_fact(row, scope) for row in rows]


def _scoped(scope: MemoryScope):
    """One scope's rows, named column by column.

    The subject clause is written as an equality or an `IS NULL` rather than as
    a parameter that might be null: a bound `NULL` compares as unknown, and the
    shared read would silently return nothing while looking correct.
    """
    query = select(MemoryRow).where(
        MemoryRow.workspace_id == scope.workspace_id,
        MemoryRow.agent_id == scope.agent_id,
        MemoryRow.kind == scope.kind.value,
    )
    if scope.subject is None:
        return query.where(
            MemoryRow.subject_type.is_(None), MemoryRow.subject_id.is_(None)
        )
    return query.where(
        MemoryRow.subject_type == scope.subject.caller_type.value,
        MemoryRow.subject_id == scope.subject.caller_id,
    )


def _fact(row: MemoryRow, scope: MemoryScope) -> RememberedFact:
    return RememberedFact(
        id=row.id, scope=scope, body=row.body, created_at=row.created_at
    )


def scope_of(row: MemoryRow) -> MemoryScope:
    """A row's own scope, rebuilt from its columns.

    Used where a row is read outside a scoped query — the self-service and
    erasure paths — so those cannot accidentally describe a row as belonging
    somewhere it does not.
    """
    if row.kind == MemoryKind.SHARED.value:
        return MemoryScope.shared(
            workspace_id=row.workspace_id, agent_id=row.agent_id
        )
    return MemoryScope.private(
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        subject=CallerIdentity(
            caller_type=CallerType(str(row.subject_type)),
            caller_id=_uuid(row.subject_id),
        ),
    )


def _uuid(value: UUID | None) -> UUID:
    if value is None:  # pragma: no cover - the CHECK forbids it
        raise ValueError("a private memory row has no subject")
    return value
