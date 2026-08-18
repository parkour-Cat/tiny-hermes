"""How a Run opens a skill proposal, and the little it is allowed to learn.

One method, and it returns either an id or a sentence. A Run does not get to
see the review queue, cannot read another proposal, and has no way to ask what
happened to the one it opened — §15.3 puts a person between a proposal and a
version, and an Agent that could watch for the approval would be one step from
being written to wait for it.

The refusal is prose rather than a code because every reason a proposal is
turned away is something the model could fix by writing different files, and
the model reads sentences.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ProposalOutcome:
    """Either it was opened, or here is why it was not."""

    proposal_id: UUID | None = None
    refusal: str | None = None


class SkillProposals(Protocol):
    async def propose(
        self,
        *,
        run_id: UUID,
        skill_version_id: UUID | None,
        files: Sequence[tuple[str, str]],
    ) -> ProposalOutcome:
        """Open one `pending` proposal from inside a Run.

        `skill_version_id` is the bound version this would change — the one
        the Agent was actually given, which is also the base the reviewer's
        diff is computed against. `None` proposes a skill that does not exist
        yet, named by the SKILL.md in `files`.
        """
        ...
