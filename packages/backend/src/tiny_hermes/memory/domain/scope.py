"""Who a memory belongs to, and the question this module refuses to answer.

Product design §14.1: private memory is isolated by `workspace + agent + end
user`. The subject here is M1's `CallerIdentity` — the roadmap's one product
assumption, and the reason is that `CallerType` grows a third member when
§4.5's end-user identity lands while this shape does not change.

**There is no way to express "every subject's memory".** Not forbidden —
unwriteable. `MemoryScope` cannot be constructed without a subject or with a
wildcard one, and the reader takes a scope rather than the parts of one. A
function that could ask an Agent for all of its private memories would be used
by a report on its first day, and the leak §14.1 exists to prevent is not a
leak anybody would notice.

Shared memory is the same table with no subject at all, and it is a different
*kind* rather than a wider scope — `shared()` is its own constructor, so
"belongs to nobody in particular" can never be reached by widening a private
scope by accident.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from tiny_hermes.runs.domain.models import CallerIdentity


class MemoryKind(StrEnum):
    #: One subject's own. Never read by any other subject, in any Run.
    PRIVATE = "private"
    #: The Agent's, and every end user may be affected by it. §14.2 gives it
    #: exactly two ways in: an administrator's own edit, or an approved
    #: proposal. No Run writes one.
    SHARED = "shared"


class MemoryStatus(StrEnum):
    #: Proposed and waiting for somebody. Never reaches a model's context —
    #: that is the whole difference between proposing and remembering.
    PENDING = "pending"
    #: In force. Read into the memory segment when it is relevant enough.
    ACTIVE = "active"
    #: Refused by a person, or withdrawn. Kept rather than deleted so a
    #: reviewer can see that the same thing was proposed twice.
    REJECTED = "rejected"


@dataclass(frozen=True)
class MemoryScope:
    """Where one memory lives.

    Frozen and complete: an instance is either one subject's private scope or
    an Agent's shared one, and there is no third state and no partial one.
    """

    workspace_id: UUID
    agent_id: UUID
    kind: MemoryKind
    #: `None` exactly when `kind` is shared. Enforced below rather than left to
    #: a caller's discipline, because the two mistakes it prevents — a private
    #: memory with no owner, a shared one attributed to somebody — are both
    #: silent.
    subject: CallerIdentity | None = None

    def __post_init__(self) -> None:
        if self.kind is MemoryKind.PRIVATE and self.subject is None:
            raise ValueError("a private memory belongs to somebody")
        if self.kind is MemoryKind.SHARED and self.subject is not None:
            raise ValueError("a shared memory belongs to the Agent, not a subject")

    @classmethod
    def private(
        cls, *, workspace_id: UUID, agent_id: UUID, subject: CallerIdentity
    ) -> "MemoryScope":
        return cls(
            workspace_id=workspace_id,
            agent_id=agent_id,
            kind=MemoryKind.PRIVATE,
            subject=subject,
        )

    @classmethod
    def shared(cls, *, workspace_id: UUID, agent_id: UUID) -> "MemoryScope":
        """The Agent's own. Its own constructor on purpose: "belongs to nobody
        in particular" must not be reachable by widening a private scope."""
        return cls(
            workspace_id=workspace_id, agent_id=agent_id, kind=MemoryKind.SHARED
        )

    def covers(self, other: "MemoryScope") -> bool:
        """Whether a memory in `other` may be read by a Run in this scope.

        Only ever true for the identical scope. Not a hierarchy and not a
        containment test that could be widened later: reading is exact, and a
        Run reads its own private scope and its Agent's shared one as two
        separate reads rather than as one query that covers both.
        """
        return self == other


def scopes_for_run(
    *, workspace_id: UUID, agent_id: UUID, subject: CallerIdentity
) -> tuple[MemoryScope, MemoryScope]:
    """The two scopes one Run may read, in the order they are read.

    Private first, because a subject's own statement about themselves outranks
    what the workspace decided about everybody — and because when the segment
    is over budget the tail is what goes, so the order here is also the order
    of what survives.
    """
    return (
        MemoryScope.private(
            workspace_id=workspace_id, agent_id=agent_id, subject=subject
        ),
        MemoryScope.shared(workspace_id=workspace_id, agent_id=agent_id),
    )
