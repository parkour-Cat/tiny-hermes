"""The gate a round asks before a call that needs a person.

Its own session and its own transaction, for the reason `SqlSkillProposals`
uses one: this happens in the middle of answering a tool call, and joining the
slice's transaction would hold a write open across a model round.

The honest consequence is the mirror of that module's. A round that asked for
an approval and was then rolled back leaves the request behind, pointing at a
Run whose turn is not in the transcript. That is the right side of the trade:
an approval too many is a row somebody rejects, while an approval that vanished
because a container failed to freeze is a Run that stopped and nobody was ever
asked why.

Reading and asking happen in one call. Between a read that says "nobody has
been asked" and a write that asks, a Worker that died would leave exactly that
Run — stopped, waiting, with nothing for anyone to answer.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.runs.domain.approval import (
    Approval,
    ApprovalStatus,
    ApprovalType,
    NormalizedCall,
    expires_at,
    is_still_valid,
)
from tiny_hermes.runs.infrastructure.tables import ApprovalRow, RunRow
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalVerdict
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow, WorkspaceRow


class SqlApprovalGate:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

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
        async with self._sessions.begin() as session:
            run = await session.get(RunRow, run_id)
            if run is None:  # pragma: no cover - the Worker holds its lease
                return ApprovalCheck(
                    ApprovalVerdict.UNAVAILABLE, detail="this Run no longer exists"
                )
            rows = (
                await session.scalars(
                    select(ApprovalRow)
                    .where(ApprovalRow.run_id == run_id)
                    .order_by(ApprovalRow.created_at.desc())
                )
            ).all()
            now = datetime.now(UTC)
            for row in rows:
                approval = _approval(row)
                if is_still_valid(approval, call, now):
                    # A person already said yes to exactly this. §16.3's
                    # invalidation rule is entirely in the hash comparison:
                    # change any argument and this stops matching.
                    return ApprovalCheck(
                        ApprovalVerdict.APPROVED, approval.id, approval.expires_at
                    )
            pending = next(
                (row for row in rows if row.status == ApprovalStatus.PENDING.value),
                None,
            )
            if pending is not None:
                # Somebody is already being asked about this Run. Even when the
                # question is about a different call — a model that changed its
                # mind while a person was deciding — no second row is written:
                # two rows a person could answer differently is a state nothing
                # downstream knows how to read.
                return ApprovalCheck(
                    ApprovalVerdict.PENDING, pending.id, pending.expires_at
                )

            asked = await self._subject(session, run, approval_type)
            if asked is None:
                return ApprovalCheck(
                    ApprovalVerdict.UNAVAILABLE,
                    detail=(
                        "this Run has no end user who could confirm it"
                        if approval_type is ApprovalType.USER_CONFIRMATION
                        else "this workspace has no administrator to ask"
                    ),
                )
            configured = await session.scalar(
                select(WorkspaceRow.approval_validity_seconds).where(
                    WorkspaceRow.id == run.workspace_id
                )
            )
            row = ApprovalRow(
                id=uuid4(),
                workspace_id=run.workspace_id,
                run_id=run_id,
                approval_type=approval_type.value,
                status=ApprovalStatus.PENDING.value,
                tool=tool,
                call_id=call_id,
                content_hash=call.content_hash,
                document=call.document,
                required_permission=required_permission,
                requested_by=asked,
                expires_at=expires_at(now, _window(configured)),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # The partial unique index caught a second Worker asking about
                # the same Run. Reported as pending rather than as an error:
                # from this Run's side the outcome is the same, and it is true.
                await session.rollback()
                return ApprovalCheck(ApprovalVerdict.PENDING)
            return ApprovalCheck(
                ApprovalVerdict.REQUESTED, row.id, row.expires_at
            )

    async def _subject(
        self, session: AsyncSession, run: RunRow, approval_type: ApprovalType
    ) -> UUID | None:
        """Who this platform asks, or `None` when there is nobody.

        A `user_confirmation` has exactly one possible subject and it is
        recorded on the Run. A `governance_approval` is asked of the
        workspace, and any of its administrators may answer — the row names
        one so the request has an addressee, and `may_decide` lets any of them
        decide it.
        """
        if approval_type is ApprovalType.USER_CONFIRMATION:
            return run.end_user_id
        return await session.scalar(
            select(MembershipRow.user_id)
            .where(
                MembershipRow.workspace_id == run.workspace_id,
                MembershipRow.role == "workspace_admin",
            )
            .order_by(MembershipRow.created_at)
            .limit(1)
        )


def _window(seconds: int | None) -> timedelta | None:
    return None if seconds is None else timedelta(seconds=seconds)


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
