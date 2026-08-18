"""The Run's session search, with its subject read off the Run.

Its own session and its own transaction, the reason `SqlSkillProposals` gives:
this answers a tool call in the middle of a round, and joining the slice's
transaction would hold it open across a model call. This one only reads, so the
trade is smaller — there is nothing here a rolled-back round could leave behind.

The subject is the Session's `CallerIdentity`, the same identity memories are
filed under. A Run searches what this person said and nothing else; a Session
that is gone yields nothing rather than everything.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.memory.domain.search import SearchHit, SearchRequest
from tiny_hermes.memory.infrastructure.sql_search import SqlSessionSearch
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow


class SqlRunSessionSearches:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def for_run(
        self, *, run_id: UUID, request: SearchRequest
    ) -> Sequence[SearchHit]:
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(
                        RunRow.workspace_id,
                        SessionRow.caller_type,
                        SessionRow.caller_id,
                    )
                    .join(SessionRow, SessionRow.id == RunRow.session_id)
                    .where(RunRow.id == run_id)
                )
            ).first()
            if found is None:  # pragma: no cover - the Worker holds this Run
                return ()
            workspace_id, caller_type, caller_id = found
            return await SqlSessionSearch(session).for_subject(
                workspace_id,
                CallerIdentity(
                    caller_type=CallerType(caller_type), caller_id=caller_id
                ),
                request,
                # A Run does not find what it just said: its own turns are
                # already in its context, and searching itself would spend the
                # page on text the model is holding.
                excluding_run=run_id,
            )
