"""Searching past sessions, scoped to what the asker may read.

Product design §14.3 and §4.6. Two callers with two different scopes, and they
are two methods rather than one method with a flag — a flag is the kind of
argument that gets passed wrong once and leaks a workspace's conversations to
one subject.

`for_subject` is what a Run's `session.search` tool uses: this workspace, this
subject's own sessions, and nothing else. `for_workspace` is the console's, and
the route gates it on §4.6 before calling.

Redacted messages are never returned by either. A message somebody had removed
is not one a search may hand back through a side door.
"""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.memory.domain.search import SearchHit, SearchRequest, snippet_of
from tiny_hermes.memory.infrastructure.search_query import matching
from tiny_hermes.runs.domain.models import CallerIdentity
from tiny_hermes.runs.infrastructure.tables import SessionMessageRow, SessionRow

#: The configuration the stored index was built with. Reading it from one place
#: keeps the query and the column from drifting into disagreeing about what a
#: word is.
SEARCH_CONFIG = "simple"


class SqlSessionSearch:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_subject(
        self,
        workspace_id: UUID,
        subject: CallerIdentity,
        request: SearchRequest,
        *,
        excluding_run: UUID | None = None,
    ) -> Sequence[SearchHit]:
        """This subject's own **past** sessions, in this workspace.

        The subject clause names both columns, so a `caller_id` that happens to
        collide across two directories is still two people.

        `excluding_run` leaves out the Run doing the searching. §14.3 is about
        retrieving what was said *before*, and a Run's own turns are already in
        its context — finding them again would spend the page on text the model
        is holding, and would make "nothing matched" impossible to ever see.
        """
        query = _base(workspace_id, request).where(
            SessionRow.caller_type == subject.caller_type.value,
            SessionRow.caller_id == subject.caller_id,
        )
        if excluding_run is not None:
            query = query.where(
                (SessionMessageRow.source_run_id.is_(None))
                | (SessionMessageRow.source_run_id != excluding_run)
            )
        return await self._hits(query, request)

    async def for_workspace(
        self, workspace_id: UUID, request: SearchRequest
    ) -> Sequence[SearchHit]:
        """Every session in this workspace. §4.6 decides who may ask, and the
        route answers that before this is called."""
        return await self._hits(_base(workspace_id, request), request)

    async def _hits(
        self, query: Select[tuple[SessionMessageRow]], request: SearchRequest
    ) -> Sequence[SearchHit]:
        rank = func.ts_rank(
            SessionMessageRow.search,
            matching(request.query),
        )
        rows = (
            await self._session.scalars(
                query.order_by(rank.desc(), SessionMessageRow.sequence.desc()).limit(
                    request.limit
                )
            )
        ).all()
        return [_hit(row) for row in rows]


def _base(
    workspace_id: UUID, request: SearchRequest
) -> Select[tuple[SessionMessageRow]]:
    """Messages in this workspace that match, and are not redacted."""
    return (
        select(SessionMessageRow)
        .join(SessionRow, SessionRow.id == SessionMessageRow.session_id)
        .where(
            SessionMessageRow.workspace_id == workspace_id,
            SessionMessageRow.redacted.is_(False),
            SessionMessageRow.search.op("@@")(
                matching(request.query)
            ),
        )
    )


def _hit(row: SessionMessageRow) -> SearchHit:
    parts: list[Any] = row.content.get("parts") or []
    body = " ".join(
        str(cast(dict[str, Any], part).get("text", ""))
        for part in parts
        if isinstance(part, dict)
    )
    snippet, shortened = snippet_of(body)
    return SearchHit(
        session_id=str(row.session_id),
        run_id=None if row.source_run_id is None else str(row.source_run_id),
        sequence=row.sequence,
        role=row.role,
        snippet=snippet,
        shortened=shortened,
    )
