"""What a workspace does with a memory somebody proposed.

Product design §14.1 gives three choices and this module is all three, as one
pure decision. It is here rather than in the service because "what happens to a
candidate" is the question the whole feature turns on, and a rule that lives
inside a database transaction is one nobody reads before changing.

**A candidate is not a memory.** `memory.remember` proposes; this decides. The
distinction is the reason §14.2 can say raw user messages never become shared
memory — a captured message can only be discovered afterwards, while a
candidate can be reviewed, refused and corrected.

**`off` stores nothing.** Not a rejected row, not a record that something was
proposed. A workspace that turned memory off did not ask for a queue of things
it is not going to read, and a row that exists is a row somebody has to decide
about later.
"""

from dataclasses import dataclass
from enum import StrEnum

from tiny_hermes.memory.domain.scope import MemoryKind, MemoryStatus

#: The most one candidate may be. Long enough for a preference or a fact,
#: short enough that nobody files a document here — the memory segment carries
#: these every round, and a paragraph in it is a paragraph the model pays for
#: on every turn for as long as the memory lives.
MAX_BODY_LENGTH = 500


class MemoryPolicy(StrEnum):
    """§14.1's three, in order of how much they let through."""

    #: Automatic memory is turned off. A candidate is refused and nothing is
    #: written — see the module docstring.
    OFF = "off"
    #: Everything waits for somebody. The default, and the only one under
    #: which something is both written down and looked at.
    ALL_PENDING = "all_pending"
    #: Low-risk private candidates are written after the rule check; the rest
    #: still wait. A widening of `all_pending`, never a bypass of it.
    LOW_RISK_AUTO = "low_risk_auto"


class CandidateOutcome(StrEnum):
    #: Nothing was written and nothing will be.
    REFUSED = "refused"
    #: Written as `pending`; a person decides.
    PENDING = "pending"
    #: Written as `active` after the rule check found it low risk.
    WRITTEN = "written"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: CandidateOutcome
    #: Why, in words a model can act on. Empty when the candidate was stored.
    reason: str = ""

    @property
    def stores_anything(self) -> bool:
        return self.outcome is not CandidateOutcome.REFUSED


def decide(
    policy: MemoryPolicy, kind: MemoryKind, *, low_risk: bool
) -> PolicyDecision:
    """What happens to one candidate.

    `low_risk` is the rule check's answer and is only consulted under
    `low_risk_auto`. It is a parameter rather than something computed here so
    the policy stays readable as three branches, and so a change to the rules
    cannot quietly change what a policy means.

    **Shared candidates never take the automatic path**, whatever the policy
    says. §14.2 gives shared memory exactly two ways in — an administrator's
    edit and an approved proposal — and neither of them is a rule.
    """
    if policy is MemoryPolicy.OFF:
        return PolicyDecision(
            CandidateOutcome.REFUSED,
            "this workspace has automatic memory turned off",
        )
    if kind is MemoryKind.SHARED:
        return PolicyDecision(CandidateOutcome.PENDING)
    if policy is MemoryPolicy.LOW_RISK_AUTO and low_risk:
        return PolicyDecision(CandidateOutcome.WRITTEN)
    return PolicyDecision(CandidateOutcome.PENDING)


def status_for(outcome: CandidateOutcome) -> MemoryStatus:
    """The row a decision produces. `refused` has none — see `stores_anything`."""
    if outcome is CandidateOutcome.WRITTEN:
        return MemoryStatus.ACTIVE
    return MemoryStatus.PENDING
