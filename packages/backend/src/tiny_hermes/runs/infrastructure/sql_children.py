"""A Run's delegation, in a transaction of its own.

Its own session for the reason `SqlMemoryCandidates` gives, and one more that is
specific to this: a child Run has to be **visible to other Workers** to be
claimed, and the parent's slice transaction does not commit until the slice
ends. Children created inside it would sit unclaimable until their parent
stopped working, which is the opposite of running in parallel with it.

The honest cost is the same one the memory write accepts, and it is worth
stating plainly. A round that delegated and was then rolled back leaves the
children behind, pointing at a parent whose turn is not in the transcript — so
the parent will ask again and get a second set. Section 4 of this phase makes
*result delivery* idempotent; creation is not, and the reason it is acceptable
is that both sets share one root budget: a duplicate delegation spends the same
counters and stops against the same ceiling rather than doubling it.

Nothing is decided here. Depth, bindings, parallel ceiling and the scope
intersection are all settled in `SqlRunStore.delegate_children`, so there is one
place a child Run comes into existence.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.children import DelegationRequest, DelegationResult


class SqlChildRuns:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def delegate(
        self, *, parent_run_id: UUID, requests: tuple[DelegationRequest, ...]
    ) -> DelegationResult:
        async with self._sessions.begin() as session:
            return await SqlRunStore(session).delegate_children(
                parent_run_id=parent_run_id, requests=requests
            )
