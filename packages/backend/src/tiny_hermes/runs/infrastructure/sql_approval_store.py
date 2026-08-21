"""Reading approvals and writing one decision, in the Run's own transaction.

Separate from `SqlApprovalGate`, which the Worker uses in the middle of a
round and which therefore opens its own session. This one is a request's
adapter: the decision and the Run's transition are one transaction, so a Run
whose question was answered can never be left parked with the answer already
written.

The transition goes through `SqlRunStore.apply_signal` rather than through an
update here. That is the one seam every state change in this platform passes,
and an approval that moved a Run by hand would be the exception nobody
remembers when the matrix changes.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.runs.domain.approval import Approval, ApprovalStatus, ApprovalType
from tiny_hermes.runs.domain.models import (
    PauseReason,
    RunCapabilities,
    RunSignal,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.infrastructure.tables import ApprovalRow, RunRow
from tiny_hermes.runs.ports.store import ApplySignalCommand
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow

#: What a decision is allowed to do to a Run. Not the caller's capabilities:
#: whether this *person* may decide was settled by `may_decide` before anything
#: reached here, and this is only the machine's own gate on the transition.
DECIDING = RunCapabilities(can_control=True, can_retry=False)


class SqlApprovalStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._runs = SqlRunStore(session)

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def list_pending(self, workspace_id: UUID) -> Sequence[Approval]:
        rows = (
            await self._session.scalars(
                select(ApprovalRow)
                .where(
                    ApprovalRow.workspace_id == workspace_id,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                )
                # Oldest first: a queue people work through, not a feed.
                .order_by(ApprovalRow.created_at)
            )
        ).all()
        return [_approval(row) for row in rows]

    async def list_pending_for_end_user(
        self, workspace_id: UUID, end_user_id: UUID
    ) -> Sequence[Approval]:
        """Plan §10. `approval_type` is filtered explicitly rather than left
        to `requested_by` alone to carry the whole guarantee: `requested_by`
        for a `governance_approval` is a workspace administrator's
        `users.id`, which cannot equal this `end_users.id` in practice, but
        an explicit filter is what `runs/domain/approval.py::may_decide`
        also does on the decide side, and this list should refuse the same
        way the decision it lists would.
        """
        rows = (
            await self._session.scalars(
                select(ApprovalRow)
                .where(
                    ApprovalRow.workspace_id == workspace_id,
                    ApprovalRow.approval_type == ApprovalType.USER_CONFIRMATION.value,
                    ApprovalRow.requested_by == end_user_id,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                )
                .order_by(ApprovalRow.created_at)
            )
        ).all()
        return [_approval(row) for row in rows]

    async def get(self, approval_id: UUID) -> Approval | None:
        row = await self._session.get(ApprovalRow, approval_id)
        return None if row is None else _approval(row)

    async def end_user_of(self, run_id: UUID) -> UUID | None:
        return await self._session.scalar(
            select(RunRow.end_user_id).where(RunRow.id == run_id)
        )

    async def decide(
        self,
        *,
        approval_id: UUID,
        status: ApprovalStatus,
        decided_by: UUID,
        decided_at: datetime,
        reason: str | None,
    ) -> Approval | None:
        row = await self._session.get(ApprovalRow, approval_id)
        if row is None:  # pragma: no cover - the service read it first
            return None
        row.status = status.value
        row.decided_by = decided_by
        row.decided_at = decided_at
        row.decision_reason = reason
        await self._session.flush()
        await self._move_run(row, status)
        return _approval(row)

    async def _move_run(self, row: ApprovalRow, status: ApprovalStatus) -> None:
        """Approved sends the Run back to work; rejected stops it and says why.

        A Run that is no longer waiting is left alone rather than forced. That
        happens when the scheduler expired it a moment earlier, and forcing the
        transition would turn a race into an error for whoever clicked.
        """
        run = await self._session.get(RunRow, row.run_id)
        if run is None or run.status != "waiting_approval":
            return
        signal = (
            RunSignal.APPROVAL_APPROVED
            if status is ApprovalStatus.APPROVED
            else RunSignal.APPROVAL_PAUSED
        )
        await self._runs.apply_signal(
            ApplySignalCommand(
                workspace_id=row.workspace_id,
                run_id=row.run_id,
                signal=signal,
                request_id=f"approval-{row.id}",
                capabilities=DECIDING,
                pause_reason=(
                    None
                    if status is ApprovalStatus.APPROVED
                    else PauseReason.APPROVAL_REJECTED
                ),
                payload={"approval_id": str(row.id)},
            )
        )

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        actor_type: str,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        # Task-9 review finding C: this used to hardcode "user" regardless of
        # who decided. An end user answering their own `user_confirmation`
        # (`end_user_approval_routes.py`) has `actor_id` pointing into
        # `end_users`, not `users` — `ApprovalService.decide` is now the one
        # place that decides which type actually applies, and this store
        # writes whatever it is told.
        self._session.add(
            AuditEventRow(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type="approval",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )
        await self._session.flush()


def _approval(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        approval_type=ApprovalType(row.approval_type),
        status=ApprovalStatus(row.status),
        tool=row.tool,
        content_hash=row.content_hash,
        document=row.document,
        required_permission=row.required_permission,
        requested_by=row.requested_by,
        expires_at=row.expires_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
    )
