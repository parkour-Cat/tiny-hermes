"""Where a Run gets memory from, and the only shape that question has.

One method, and it takes a **scope** rather than the parts of one. That is the
whole isolation control expressed as a type: a caller cannot ask this port for
"the memories of this Agent" because there is no argument for it, and
`MemoryScope` cannot be built without a subject unless it is deliberately the
shared one.

`pending` is never returned. A candidate that reached a model's context would
have been remembered without anybody agreeing, which is the difference between
proposing and remembering and the reason §14.1 has policies at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.memory.domain.scope import MemoryScope


@dataclass(frozen=True)
class RememberedFact:
    """One memory, as a round needs it.

    `body` is what enters the context. `scope` travels with it so a reader —
    the planner, a person looking at a Run — can say whether a line came from
    this person or from the workspace, which are different claims.
    """

    id: UUID
    scope: MemoryScope
    body: str
    created_at: datetime


class MemoryLibrary(Protocol):
    async def active_in(
        self, scope: MemoryScope, *, limit: int
    ) -> Sequence[RememberedFact]:
        """This scope's memories, newest first, at most `limit` of them.

        The recency read, used where there is no query to rank against. It
        guarantees only that nothing outside the scope and nothing unapproved
        comes back.
        """
        ...

    async def relevant_in(
        self, scope: MemoryScope, query: str, *, limit: int
    ) -> Sequence[RememberedFact]:
        """This scope's memories, most relevant to `query` first.

        Relevance is **keyword matching, not meaning** — a PostgreSQL full-text
        rank, broken by recency. §14.3 excludes vector memory on purpose, and
        the name says `query` rather than anything that implies understanding.
        A blank query falls back to recency, so a Run whose input is empty
        still gets its most recent memories rather than none.
        """
        ...
