"""Whether this call may proceed, asked of whatever holds the approvals.

One method, because a round asks one question: *may I make this call now?* The
four answers are the four things that can be true, and they are separate
because the Run does something different for each — proceed, stop and wait,
stop because somebody is already being asked, or stop because there is nobody
to ask.

That last one is `unavailable`, and it is not an error. §16.3: a Run that
reaches a `user_confirmation` with no EndUser to confirm it enters
`paused(approval_unavailable)` — it does not silently escalate to an
administrator and nobody decides on the user's behalf.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from tiny_hermes.runs.domain.approval import ApprovalType, NormalizedCall


class ApprovalVerdict(StrEnum):
    #: A person already approved this exact call and it has not run out.
    APPROVED = "approved"
    #: Nobody had been asked; this round asked. The Run waits.
    REQUESTED = "requested"
    #: Somebody is already being asked about this Run. The Run waits, and no
    #: second row is written — two rows a person could answer differently is a
    #: state nothing downstream knows how to read.
    PENDING = "pending"
    #: There is no subject who may decide this. The Run pauses; it does not
    #: escalate and nobody decides in the absent person's name.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ApprovalCheck:
    verdict: ApprovalVerdict
    approval_id: UUID | None = None
    #: When the pending question runs out. The Run waits until exactly this
    #: moment, so the deadline a person sees on the approval and the deadline
    #: the scheduler sweeps are one number rather than two that drift.
    expires_at: datetime | None = None
    #: Why, when the answer is `unavailable`. Read by a person, so it says what
    #: is missing rather than naming a branch.
    detail: str = ""

    @property
    def proceeds(self) -> bool:
        return self.verdict is ApprovalVerdict.APPROVED


class ApprovalGate(Protocol):
    async def check(
        self,
        *,
        run_id: UUID,
        approval_type: ApprovalType,
        tool: str,
        call_id: str,
        call: NormalizedCall,
        required_permission: str | None,
    ) -> ApprovalCheck:
        """Whether this call may proceed, and ask if nobody has been asked.

        Asking is part of the same call rather than a second one: between a
        read that says "no approval" and a write that creates one, a crashed
        Worker would leave a Run that stopped without anybody being asked.
        """
        ...
