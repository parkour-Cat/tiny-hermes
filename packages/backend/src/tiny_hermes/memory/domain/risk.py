"""Whether a memory candidate is safe enough to write without asking.

Product design §14.1's third policy, `low_risk_auto`, needs a definition of
"low risk", and this is it — pure, and readable as a list a person can argue
with. It is a **heuristic, not a guarantee**: it decides what may skip the
queue, never what is true, and its default answer is "not low risk". The whole
module is built so that the way to be wrong is to make a person look, which is
the cheap mistake.

Three things make a candidate high risk, and any one of them is enough:

- **It carries a secret or an identifier.** The same patterns the skill
  scanner blocks on, because a credential is no safer in a memory than in a
  skill — and a memory is read into a model's context every round it lives.
- **It is about somebody else.** A memory is keyed to one subject; one that
  names another person, an email or a phone number is a note about them filed
  under someone who is not them. Those wait for a person however innocuous they
  read.
- **It reaches for power.** Words about permissions, roles, access and identity
  are how a preference smuggles in a policy change, so a candidate that talks
  about them is never low risk on its own say-so.

A candidate that trips none of these, and is short, and reads as a first-person
preference, is low risk. Everything else waits.
"""

import re
from dataclasses import dataclass

#: What a low-risk candidate may weigh. Shorter than the domain's hard limit on
#: purpose: the automatic path is for a preference or a fact in a sentence, and
#: a candidate approaching the ceiling is long enough that a person should see
#: it before it is read every round for as long as it lives.
LOW_RISK_MAX_LENGTH = 200

#: A secret or a long token assigned to a telling name. The skill scanner's
#: list, kept in step with it deliberately: a credential is no safer here.
_SECRETS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9/+_.-]{20,}"
    ),
)

#: An email or a phone number: a contact detail is a fact about a person, and a
#: candidate is filed under one subject.
_CONTACT: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?\d[\s-]?){9,}\d(?!\d)"),
)

#: Words a preference has no business carrying. Matched whole, case-insensitive:
#: "I prefer terse answers" is fine, "give me admin" is not.
_POWER = re.compile(
    r"(?i)\b(admin|administrator|permission|permissions|role|roles|grant|"
    r"privilege|privileges|access|sudo|root|credential|credentials|"
    r"password|api[_-]?key|token|secret)\b"
)

#: The one shape the automatic path accepts. A first-person statement about the
#: person themselves — a preference or a durable fact — rather than a note about
#: the world. Not proof of anything; a candidate that does not even read this
#: way has not made the weak case the automatic path needs.
_FIRST_PERSON = re.compile(
    r"(?i)\b(i|i'm|i am|my|me|mine|we|our|us)\b"
)


class RiskLevel:
    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True)
class RiskVerdict:
    low_risk: bool
    #: Why it is high risk, for a reviewer and for the audit trail. Empty when
    #: low. Never the candidate's own text — a reason that quoted a secret would
    #: leak it into a place the queue exists to keep it out of.
    reason: str = ""


def assess(body: str) -> RiskVerdict:
    """The one entry point. Its default is high risk; every `return LOW` is a
    case that had to be argued for.

    Order matters only for which reason a reviewer sees first, not for the
    answer: any single high-risk finding is enough, so the checks run cheapest
    and most alarming first.
    """
    text = body.strip()
    if not text:
        # Nothing to write. Not low risk, because "write an empty memory" is not
        # a request the automatic path should honour silently.
        return RiskVerdict(low_risk=False, reason="empty")
    if len(text) > LOW_RISK_MAX_LENGTH:
        return RiskVerdict(low_risk=False, reason="too long for the automatic path")
    if any(pattern.search(text) for pattern in _SECRETS):
        return RiskVerdict(low_risk=False, reason="looks like it carries a secret")
    if _POWER.search(text):
        return RiskVerdict(low_risk=False, reason="mentions permissions or identity")
    if any(pattern.search(text) for pattern in _CONTACT):
        return RiskVerdict(
            low_risk=False, reason="looks like it is about another person"
        )
    if not _FIRST_PERSON.search(text):
        return RiskVerdict(
            low_risk=False, reason="not a first-person preference or fact"
        )
    return RiskVerdict(low_risk=True)
