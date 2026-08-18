"""A Run's way of looking back through its own past conversations.

One method, and the scope is **not** an argument. A Run searches the sessions of
the subject it is working with; the adapter reads that off the Run, so there is
no parameter a caller could point somewhere else. §14.3 gives the model a way to
retrieve on demand, not a way to choose whose history it retrieves.

What comes back is snippets. The bounds are in `memory/domain/search.py`, and
they refuse rather than truncate silently — a model handed half a message cannot
tell that is what it is holding, so a shortened snippet says so.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from tiny_hermes.memory.domain.search import SearchHit, SearchRequest


class SessionSearches(Protocol):
    async def for_run(
        self, *, run_id: UUID, request: SearchRequest
    ) -> Sequence[SearchHit]:
        """Past messages this Run's own subject said, most relevant first."""
        ...
