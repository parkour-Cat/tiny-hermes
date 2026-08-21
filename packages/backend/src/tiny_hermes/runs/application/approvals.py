"""Reading pending approvals, and deciding one.

Product design §16.3. The rules about *who* live in `runs/domain/approval.py`
and are only applied here; this module's own job is the three things a decision
touches at once — the approval row, the Run's state, and the audit trail.

**A decision moves the Run.** Approving sends it back to `queued`; rejecting
pauses it with `approval_rejected`. Doing one without the other would leave a
Run parked in `waiting_approval` with an answered question, which is the same
from the outside as a Run nobody answered.

**A rejection must say why.** The person whose Run stopped is not the person
who stopped it, and "no" without a reason gives them nothing to change.

**Nothing here overwrites a decision.** §16.3 is explicit that a management
override is a *new* governance approval with its reasons written down, never a
rewrite of somebody's confirmation. So a decided approval is never decided
again, and the refusal says so.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.runs.domain.approval import (
    Approval,
    ApprovalDecision,
    ApprovalStatus,
    Decider,
    may_decide,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

#: How long a rejection reason may be. Long enough for a paragraph, short
#: enough that nobody pastes a log into it.
MAX_REASON_LENGTH = 2048


class ApprovalStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def list_pending(self, workspace_id: UUID) -> Sequence[Approval]: ...

    async def get(self, approval_id: UUID) -> Approval | None: ...

    async def end_user_of(self, run_id: UUID) -> UUID | None: ...

    async def decide(
        self,
        *,
        approval_id: UUID,
        status: ApprovalStatus,
        decided_by: UUID,
        decided_at: datetime,
        reason: str | None,
    ) -> Approval | None:
        """Write the decision and move the Run in one transaction.

        One method rather than two, because a decision that landed without the
        Run moving is a Run parked in `waiting_approval` with its question
        already answered — indistinguishable from one nobody answered.
        """
        ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        # Task-9 review finding C: no default, matching
        # `SubjectStore.append_audit`'s own reasoning — a caller that omits
        # this is a caller that has not decided who acted.
        actor_type: str,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class ApprovalError(Exception):
    """Base for every expected refusal here."""


class UnknownApproval(ApprovalError):
    pass


class ForbiddenApprovalAction(ApprovalError):
    """This person may not decide this approval.

    Both directions of §16.3 arrive here: an administrator reaching for
    somebody's `user_confirmation`, and an end user reaching for a governance
    approval. The message does not distinguish them — from outside, "you may
    not" is the whole answer.
    """


class ApprovalAlreadyDecided(ApprovalError):
    def __init__(self, status: ApprovalStatus) -> None:
        super().__init__(f"this approval was already {status.value}")
        self.status = status


class ApprovalExpired(ApprovalError):
    """Answered after it ran out. Refused rather than honoured late: the Run
    has already been paused, and an approval that resumed it would resume work
    whose context nobody has looked at since."""


class ReasonRequired(ApprovalError):
    """A rejection with no reason. See the module docstring."""


@dataclass(frozen=True)
class ApprovalService:
    store: ApprovalStore

    async def list_pending(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> Sequence[Approval]:
        """Every approval this workspace is being asked about.

        Unfiltered by who may decide each one, on purpose: a developer who can
        see that their Run is waiting on an administrator knows to go and ask,
        while a list that hid it would leave them watching a Run that appears
        stuck for no reason. Deciding is still checked per approval.
        """
        del request_id
        role = await self.store.user_role(workspace_id, actor.id)
        if role is None and not actor.is_platform_admin:
            raise ForbiddenApprovalAction
        return await self.store.list_pending(workspace_id)

    async def decide(
        self,
        actor: Actor,
        workspace_id: UUID,
        approval_id: UUID,
        decision: ApprovalDecision,
        reason: str | None,
        request_id: str,
    ) -> Approval:
        approval = await self.store.get(approval_id)
        if approval is None or approval.workspace_id != workspace_id:
            # Another workspace's approval is not found here, the answer every
            # other catalog gives and for the same reason.
            raise UnknownApproval
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalAlreadyDecided(approval.status)
        now = datetime.now(UTC)
        if approval.expires_at <= now:
            raise ApprovalExpired
        if actor.is_service_account:
            # §16.3's subjects are people. A key that could approve would make
            # the whole section a formality that another key satisfies.
            raise ForbiddenApprovalAction
        if not may_decide(approval, await self._decider(actor, workspace_id, approval)):
            raise ForbiddenApprovalAction
        cleaned = (reason or "").strip() or None
        if decision is ApprovalDecision.REJECT and cleaned is None:
            raise ReasonRequired
        decided = await self.store.decide(
            approval_id=approval.id,
            status=(
                ApprovalStatus.APPROVED
                if decision is ApprovalDecision.APPROVE
                else ApprovalStatus.REJECTED
            ),
            decided_by=actor.id,
            decided_at=now,
            reason=cleaned[:MAX_REASON_LENGTH] if cleaned else None,
        )
        if decided is None:  # pragma: no cover - read a few lines above
            raise UnknownApproval
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            # Task-9 review finding C: this used to hardcode "user" no
            # matter who decided. `end_user_approval_routes.py` builds a
            # console-shaped `Actor` around an end user's own id specifically
            # so this method needs no separate code path — `actor.is_end_user`
            # is how that route says which subject it actually authenticated.
            actor_type="end_user" if actor.is_end_user else "user",
            action=f"approval.{decision.value}d",
            resource_id=approval.id,
            request_id=request_id,
            context={
                "approval_type": approval.approval_type.value,
                "tool": approval.tool,
                "run_id": str(approval.run_id),
            },
        )
        return decided

    async def _decider(
        self, actor: Actor, workspace_id: UUID, approval: Approval
    ) -> Decider:
        """This person, in the terms §16.3 cares about.

        Whether they are the Run's EndUser is read from the Run rather than
        assumed from the approval's `requested_by`: a row could have been
        written for somebody who has since stopped being that Run's user, and
        the Run is where that fact lives.

        Task-9 review finding D: `end_user == actor.id` alone used to be the
        whole check — an id match with no type attached. `actor.is_end_user`
        is required alongside it now, so a console `Actor` (a service account
        or a workspace member, `is_end_user` always `False`) can never be
        credited with being the Run's own end user merely because its id
        happens to equal one — the shape of bug finding D calls out: fine
        while ids are random, wrong in principle, and silently exploitable
        the day they are not.
        """
        role = await self.store.user_role(workspace_id, actor.id)
        end_user = await self.store.end_user_of(approval.run_id)
        return Decider(
            user_id=actor.id,
            is_workspace_admin=role is Role.WORKSPACE_ADMIN,
            is_platform_admin=actor.is_platform_admin,
            is_run_end_user=(
                actor.is_end_user and end_user is not None and end_user == actor.id
            ),
        )
