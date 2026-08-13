"""Whether a Run currently holds a given WorkerLease.

Read-only, and it reaches into the Run module's table on purpose rather than
going through Run Coordination: this is a question, not a transition, and a
Controller that could call into Run Coordination would be a Controller that
could change a Run's state. It can look; it cannot touch.

`now` is compared in the database rather than in Python. The Controller and
PostgreSQL are different processes with different clocks, and the lease's whole
purpose is to be the one authority on who is executing.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.runs.infrastructure.tables import WorkerLeaseRow


class SqlLeaseAuthority:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def holds(self, run_id: UUID, lease_id: UUID) -> bool:
        found = await self._session.execute(
            select(WorkerLeaseRow.id).where(
                WorkerLeaseRow.id == lease_id,
                WorkerLeaseRow.run_id == run_id,
                WorkerLeaseRow.released_at.is_(None),
                WorkerLeaseRow.expires_at > func.now(),
            )
        )
        return found.scalar_one_or_none() is not None

    async def any_live(self, run_id: UUID) -> bool:
        """Used by the Scheduler's cleanup, which must not act while one is live."""
        found = await self._session.execute(
            select(WorkerLeaseRow.id).where(
                WorkerLeaseRow.run_id == run_id,
                WorkerLeaseRow.released_at.is_(None),
                WorkerLeaseRow.expires_at > func.now(),
            )
        )
        return found.scalar_one_or_none() is not None

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
