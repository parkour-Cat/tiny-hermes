"""A Run's one way to say "this is worth remembering".

One method, and it **proposes** rather than writes. What happens next is the
workspace's policy, and this port deliberately does not let the caller choose
it: a Run that could ask for its candidate to be written would be a Run that
decides what §14.1 says the workspace decides.

The answer says which of the three outcomes happened, because the model has to
be told the truth about it. A candidate that was refused and one that is
waiting are different situations, and a model told only "done" would carry on
as though it had been remembered.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.memory.domain.policy import CandidateOutcome


@dataclass(frozen=True)
class CandidateResult:
    outcome: CandidateOutcome
    #: The row, when one was written. `None` for a refusal. A pending row is
    #: its own review handle — a memory candidate does not become a tool-call
    #: approval and does not pause a Run, so there is no separate approval id
    #: to carry. See the SQL store's note on why M2C's approvals table is not
    #: reused here.
    memory_id: UUID | None = None
    #: Why, for a refusal or for a candidate that could not be stored.
    detail: str = ""


class MemoryCandidates(Protocol):
    async def propose(self, *, run_id: UUID, body: str) -> CandidateResult:
        """Offer one candidate for this Run's own subject.

        The scope is derived from the Run rather than passed in: a Run may
        propose a memory about the person it is working with and about nobody
        else, and an argument for the subject would be an argument somebody
        could get wrong.
        """
        ...
