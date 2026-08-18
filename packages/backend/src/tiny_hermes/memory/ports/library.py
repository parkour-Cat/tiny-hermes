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

        Ordered here and ranked later: relevance needs the Run's own input,
        which this port does not have and should not learn. What it guarantees
        is that nothing outside the scope and nothing unapproved comes back.
        """
        ...
