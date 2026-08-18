"""What an approval is about, and whether an old one still covers a new call.

Product design §16.3. Everything here is pure: no store, no clock of its own,
no notion of who is asking. The three questions it answers are the three the
rest of the platform must never answer twice in two places.

**What did the person actually approve?** `normalize_call` turns a call into a
document and a hash. A person approving "write to /workspace/data/report.md"
approved that, not "whatever the model asks for next", so the hash covers the
tool name, every argument, the working directory, the target resource and the
permission being asked for. Change any one and the old approval stops
covering it.

**Is a given approval still good?** `is_still_valid` — approved, unexpired, and
about this exact call. Three ways to be stale, checked in one place, because a
caller that checked two of them would let the third through.

**May this person decide it?** `may_decide`. §16.3 gives the two approval kinds
to two different subjects: a `user_confirmation` belongs to the EndUser who
started the Run and to nobody else — an administrator deciding on their behalf
is exactly the impersonation the section forbids — and a `governance_approval`
belongs to a workspace or platform administrator, never to an end user.

**One honest limit.** The hash is computed from arguments that have already
been parsed out of JSON, so `1` and `1.0` are different (an int and a float)
while `1.0` and `1.00` are the same float and cannot be told apart. Recorded
here rather than left for somebody to discover: what binds an approval is the
value the platform will send, and by the time it is here the extra zero is
already gone.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

#: How long an approval is good for when a workspace has said nothing.
DEFAULT_VALIDITY = timedelta(hours=24)

#: The range a workspace may configure, §16.3's own numbers. The floor is not
#: decoration: an approval that expires in seconds is one a person cannot
#: actually act on, and the ceiling stops "approved once" from becoming
#: "approved indefinitely" through a settings page.
MIN_VALIDITY = timedelta(minutes=5)
MAX_VALIDITY = timedelta(days=7)


class ApprovalType(StrEnum):
    #: The EndUser's own decision about their own data. Only they may make it.
    USER_CONFIRMATION = "user_confirmation"
    #: A workspace's decision about shared or irreversible things. Only an
    #: administrator may make it, and an end user never may.
    GOVERNANCE_APPROVAL = "governance_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Written by the scheduler's sweep. Kept as its own status rather than
    #: inferred from `expires_at` on every read, so "nobody answered in time"
    #: is a fact with a moment attached rather than a comparison whose answer
    #: changes depending on when you ask.
    EXPIRED = "expired"


class ApprovalDecision(StrEnum):
    """What a person may do to a pending approval. Two, and no third.

    There is deliberately no "approve with changes": §16.3 binds an approval to
    normalized arguments, and an approval that could alter them would be a
    person authorizing a call nobody had described to them.
    """

    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class NormalizedCall:
    """One call, in the shape an approval is written against.

    `document` is what a person is shown and `content_hash` is what the
    platform compares. Both come out of the same normalization, so the thing
    reviewed and the thing enforced cannot drift apart.
    """

    document: dict[str, Any]
    content_hash: str


class CallNotNormalizable(Exception):
    """An argument with no stable spelling.

    Refused rather than coerced. A value this platform would have to invent a
    representation for is a value two normalizations could disagree about, and
    an approval hash that is not reproducible approves nothing.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def normalize_call(
    tool: str,
    arguments: Mapping[str, object],
    *,
    working_directory: str | None = None,
    target: str | None = None,
    required_permission: str | None = None,
) -> NormalizedCall:
    """What a person is approving, as a document and a hash.

    Every field §16.3 names is in it. Argument *order* is not: a mapping has no
    order the caller chose, and two spellings of one call must be one approval
    — otherwise a model that emitted its arguments in a different order the
    second time would need the same person to approve the same thing twice.

    Everything else is significant. A different working directory is a
    different file; a different target is a different resource; a different
    permission is a different power being asked for.
    """
    document = {
        "tool": tool,
        "arguments": {name: _value(name, item) for name, item in arguments.items()},
        "working_directory": working_directory,
        "target": target,
        "required_permission": required_permission,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        # Sorted, so the order the arguments arrived in is not part of what was
        # approved. Everything else about them is.
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return NormalizedCall(
        document=document, content_hash=hashlib.sha256(encoded).hexdigest()
    )


@dataclass(frozen=True)
class Approval:
    """One request for a person's decision, and what became of it."""

    id: UUID
    run_id: UUID
    workspace_id: UUID
    approval_type: ApprovalType
    status: ApprovalStatus
    #: The tool call this is about, by name. Carried beside the hash because a
    #: person reading a list needs to know what they are looking at without
    #: opening every row.
    tool: str
    content_hash: str
    #: What the person is shown: the normalized call, exactly as hashed.
    document: dict[str, Any]
    #: The permission this call needs, when it needs a named one. `None` for a
    #: `user_confirmation` about the caller's own data, which asks for no
    #: additional power.
    required_permission: str | None
    #: Who the platform asked. For a `user_confirmation` this is the Run's
    #: EndUser and nobody else may answer.
    requested_by: UUID
    expires_at: datetime
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    #: Free text from whoever decided. Required on a rejection so the person
    #: whose Run stopped is told something they can act on.
    decision_reason: str | None = None


def is_still_valid(
    approval: Approval, call: NormalizedCall, now: datetime | None = None
) -> bool:
    """Whether this approval covers this call, right now.

    Three ways to be stale and all three checked here: it was never approved,
    it has run out, or it was about a different call. Split across callers,
    one of them would eventually check two.
    """
    moment = now or datetime.now(UTC)
    if approval.status is not ApprovalStatus.APPROVED:
        return False
    if approval.expires_at <= moment:
        return False
    return approval.content_hash == call.content_hash


@dataclass(frozen=True)
class Decider:
    """Who is trying to decide, in the only terms §16.3 cares about.

    Not `Actor`: the question here is not "may this person use the platform"
    but "is this person the subject this kind of approval belongs to", and
    those are different enough that sharing a type would invite one to be
    mistaken for the other.
    """

    user_id: UUID
    is_workspace_admin: bool = False
    is_platform_admin: bool = False
    #: True when this identity is the EndUser the Run was started for. Passed
    #: in rather than derived, because "the Run's EndUser" is a fact about the
    #: Run and this module is given facts.
    is_run_end_user: bool = False


def may_decide(approval: Approval, decider: Decider) -> bool:
    """Whether this person may decide this approval. §16.3, both directions.

    A `user_confirmation` is the EndUser's own decision about their own data.
    An administrator answering it would be exactly the impersonation the
    section forbids — and the platform would have no way afterwards to say
    which of the two actually agreed. An administrator who believes the action
    should happen anyway opens a `governance_approval` and writes down why;
    they never overwrite the user's record.

    A `governance_approval` is a workspace decision, so an end user may never
    answer one regardless of how the Run reached this point.
    """
    if approval.approval_type is ApprovalType.USER_CONFIRMATION:
        return decider.is_run_end_user and decider.user_id == approval.requested_by
    return decider.is_workspace_admin or decider.is_platform_admin


def validity_window(configured: timedelta | None) -> timedelta:
    """How long a new approval is good for, inside §16.3's bounds.

    Clamped rather than refused: this is read where an approval is *created*,
    in the middle of somebody's Run, and failing a Run because a settings page
    holds an out-of-range number would punish the wrong person. The settings
    page is where an impossible value is refused.
    """
    if configured is None:
        return DEFAULT_VALIDITY
    return max(MIN_VALIDITY, min(MAX_VALIDITY, configured))


def expires_at(now: datetime, configured: timedelta | None = None) -> datetime:
    return now + validity_window(configured)


def _value(name: str, value: object) -> Any:
    """One argument, in a spelling two runs will agree on.

    Containers are walked rather than trusted: a dict inside an argument is
    sorted by the same rule the top level is, so an approval is not invalidated
    by a model that emitted the same nested object in a different order.
    """
    if value is None or isinstance(value, bool | int | float | str):
        # `bool` before `int` on purpose — `isinstance(True, int)` is true, and
        # `True` and `1` must not normalize to the same argument.
        return value
    if isinstance(value, Mapping):
        nested = cast(Mapping[Any, Any], value)
        return {
            str(key): _value(f"{name}.{key}", item) for key, item in nested.items()
        }
    if isinstance(value, list | tuple):
        # Order *is* significant inside a list: `["a", "b"]` and `["b", "a"]`
        # are different arguments to anything that reads them positionally, and
        # this module cannot know which tools do.
        entries: list[Any] = list(cast(list[Any] | tuple[Any, ...], value))
        return [_value(f"{name}[]", item) for item in entries]
    raise CallNotNormalizable(f"{name} has no stable representation")
