"""Reading, correcting and erasing what belongs to one subject.

The erasure is the part worth reading. Content deletes rather than marks, in
one transaction, in the order the foreign keys allow: approvals before the
Runs they answer, artifacts and messages before the sessions that own them,
memories on their own, the Session's own head pointer cleared before any of
it — a Run in `waiting_approval` or `paused` never releases that pointer on
its own, so erasure cannot wait for one to. After this runs there is no
content row a later query could find, which is the whole difference between
a deletion and a promise of one.

An end user's own *identity* is the one exception, and by design (§3) rather
than by omission: `end_users` carries no identifying data to begin with, so
erasing one is `erased_at` on that row and `profile` cleared on the
`external_identities` row mapped to it — see `_erase_end_user` for why that
row is updated rather than deleted. `end_user_sessions` does delete in
effect — every live row is revoked — because a cookie that outlived its own
erasure would be the one place this promise leaked.

A correction writes a second row and rejects the first, so a reviewer can see
that a memory was changed and what it used to say. The audit trail carries the
fact; the rows carry the text.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.artifacts.infrastructure.tables import ArtifactRow
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.identity.infrastructure.end_user_session_tables import EndUserSessionRow
from tiny_hermes.identity.infrastructure.end_user_tables import (
    EndUserRow,
    ExternalIdentityRow,
)
from tiny_hermes.memory.application.service import MemoryRecord
from tiny_hermes.memory.application.subject_service import ErasureReport
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryScope, MemoryStatus
from tiny_hermes.memory.infrastructure.sql_service_store import record_of
from tiny_hermes.memory.infrastructure.tables import MemoryRow
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType
from tiny_hermes.runs.infrastructure.tables import (
    ApprovalRow,
    RunRow,
    SessionMessageRow,
    SessionRow,
)
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlSubjectStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def memories_of(self, scope: MemoryScope) -> Sequence[MemoryRecord]:
        """One scope's rows, whatever their status.

        Unlike the Run's read this includes `pending` and `rejected`: a subject
        asking what is held about them is owed the candidate somebody has not
        decided yet and the one they themselves withdrew.
        """
        subject = scope.subject
        query = select(MemoryRow).where(
            MemoryRow.workspace_id == scope.workspace_id,
            MemoryRow.agent_id == scope.agent_id,
            MemoryRow.kind == scope.kind.value,
        )
        if subject is None:  # pragma: no cover - the service passes a private scope
            query = query.where(MemoryRow.subject_id.is_(None))
        else:
            query = query.where(
                MemoryRow.subject_type == subject.caller_type.value,
                MemoryRow.subject_id == subject.caller_id,
            )
        rows = (
            await self._session.scalars(query.order_by(MemoryRow.created_at))
        ).all()
        return [record_of(row) for row in rows]

    async def memories_of_subject(
        self, workspace_id: UUID, subject: CallerIdentity
    ) -> Sequence[MemoryRecord]:
        """This subject's private memory, across every Agent — no `agent_id`
        in the query at all, unlike `memories_of`. `erase`'s own count query
        a few methods down already reads this same shape (workspace, subject
        type, subject id, no Agent) for the same reason: what belongs to a
        subject was never scoped to one Agent, only ever read that way when a
        caller happened to name one.
        """
        rows = await self._session.scalars(
            select(MemoryRow)
            .where(
                MemoryRow.workspace_id == workspace_id,
                MemoryRow.kind == MemoryKind.PRIVATE.value,
                MemoryRow.subject_type == subject.caller_type.value,
                MemoryRow.subject_id == subject.caller_id,
            )
            .order_by(MemoryRow.created_at)
        )
        return [record_of(row) for row in rows.all()]

    async def get(self, memory_id: UUID) -> MemoryRecord | None:
        row = await self._session.get(MemoryRow, memory_id)
        return None if row is None else record_of(row)

    async def subject_of(self, memory_id: UUID) -> CallerIdentity | None:
        row = await self._session.get(MemoryRow, memory_id)
        if row is None or row.kind == MemoryKind.SHARED.value:
            return None
        if row.subject_type is None or row.subject_id is None:  # pragma: no cover
            return None
        return CallerIdentity(
            caller_type=CallerType(row.subject_type), caller_id=row.subject_id
        )

    async def replace(
        self, memory_id: UUID, body: str, now: datetime
    ) -> MemoryRecord:
        """A new row saying the new thing, and the old one rejected.

        Two rows rather than an edit: "this was corrected" and "this was always
        so" have to stay distinguishable, and an overwrite loses that.
        """
        old = await self._session.get(MemoryRow, memory_id)
        if old is None:  # pragma: no cover - the service read it first
            raise ValueError("no such memory")
        old.status = MemoryStatus.REJECTED.value
        old.updated_at = now
        fresh = MemoryRow(
            id=uuid4(),
            workspace_id=old.workspace_id,
            agent_id=old.agent_id,
            kind=old.kind,
            subject_type=old.subject_type,
            subject_id=old.subject_id,
            body=body,
            status=MemoryStatus.ACTIVE.value,
            origin="subject",
            origin_run_id=None,
            context={"corrects": str(memory_id)},
            created_by=None,
            updated_at=now,
        )
        self._session.add(fresh)
        await self._session.flush()
        return record_of(fresh)

    async def set_status(
        self, memory_id: UUID, status: MemoryStatus, now: datetime
    ) -> MemoryRecord | None:
        row = await self._session.get(MemoryRow, memory_id)
        if row is None:  # pragma: no cover - the service read it first
            return None
        row.status = status.value
        row.updated_at = now
        await self._session.flush()
        return record_of(row)

    async def sessions_of(
        self, workspace_id: UUID, subject: CallerIdentity
    ) -> Sequence[UUID]:
        rows = await self._session.scalars(
            select(SessionRow.id).where(
                SessionRow.workspace_id == workspace_id,
                SessionRow.caller_type == subject.caller_type.value,
                SessionRow.caller_id == subject.caller_id,
            )
        )
        return list(rows.all())

    async def erase(
        self, workspace_id: UUID, subject: CallerIdentity
    ) -> ErasureReport:
        """Delete this subject's memories, sessions, messages, Runs and files.

        In the order the foreign keys allow, and counted before each delete so
        the audit line can say what went. A Run belongs to exactly one
        Session and carries no content of its own once its messages and
        artifacts are gone, so nothing is left behind by leaving it out of
        this deletion — see the module docstring for why its Session's head
        pointer and its own `approvals` row have to go first rather than
        being assumed already clear.
        """
        sessions = list(await self.sessions_of(workspace_id, subject))
        memories = await self._count(
            select(func.count())
            .select_from(MemoryRow)
            .where(
                MemoryRow.workspace_id == workspace_id,
                MemoryRow.subject_type == subject.caller_type.value,
                MemoryRow.subject_id == subject.caller_id,
            )
        )
        await self._session.execute(
            delete(MemoryRow).where(
                MemoryRow.workspace_id == workspace_id,
                MemoryRow.subject_type == subject.caller_type.value,
                MemoryRow.subject_id == subject.caller_id,
            )
        )
        messages = 0
        artifacts = 0
        if sessions:
            messages = await self._count(
                select(func.count())
                .select_from(SessionMessageRow)
                .where(SessionMessageRow.session_id.in_(sessions))
            )
            artifacts = await self._count(
                select(func.count())
                .select_from(ArtifactRow)
                .where(ArtifactRow.session_id.in_(sessions))
            )
            await self._session.execute(
                delete(ArtifactRow).where(ArtifactRow.session_id.in_(sessions))
            )
            await self._session.execute(
                delete(SessionMessageRow).where(
                    SessionMessageRow.session_id.in_(sessions)
                )
            )
            # `fk_sessions_head_run` means a Session whose head still points
            # at one of these Runs blocks the delete below — and the head is
            # only ever released on a *terminal* transition (`_terminalize`,
            # `runs/infrastructure/sql_store.py`). `waiting_approval`,
            # `paused` and `waiting_external` are not terminal, so a subject
            # parked on an unanswered confirmation would otherwise be
            # unerasable for as long as nobody answers it. Null the pointer
            # here, unconditionally, rather than leaning on the Run having
            # already finished.
            await self._session.execute(
                update(SessionRow)
                .where(SessionRow.id.in_(sessions))
                .values(head_run_id=None)
            )
            # A Run stopped on `waiting_approval` is exactly a Run with a
            # pending row here (`fk_approvals_run`, no cascade) — the same
            # non-terminal state that leaves the head pointer set. Gone
            # before the Runs are, for the same reason.
            await self._session.execute(
                delete(ApprovalRow).where(
                    ApprovalRow.run_id.in_(
                        select(RunRow.id).where(RunRow.session_id.in_(sessions))
                    )
                )
            )
            await self._session.execute(
                delete(RunRow).where(RunRow.session_id.in_(sessions))
            )
            await self._session.execute(
                delete(SessionRow).where(SessionRow.id.in_(sessions))
            )
        if subject.caller_type is CallerType.END_USER:
            await self._erase_end_user(workspace_id, subject.caller_id)
        await self._session.flush()
        return ErasureReport(
            memories=memories,
            sessions=len(sessions),
            messages=messages,
            artifacts=artifacts,
        )

    async def _erase_end_user(self, workspace_id: UUID, end_user_id: UUID) -> None:
        """The third subject's own erasure, design §3's own promise made
        good: `end_users` carries no identifying data by default, so what
        this reaches is `erased_at` on the one row and `profile` on the
        `external_identities` row that maps to it — "the erasure only needs
        to clear one column of one table, not chase the subject everywhere"
        (design §3). Nothing else here holds a subject's identity to begin
        with.

        `external_identities` is **updated, not deleted**. That is the
        decision the review asked this module to make explicit: a subject
        who returns with the same enterprise credential after being erased
        is *refused*, never handed a fresh `EndUser` — and refusing them
        depends on `upsert_external_identity` finding this same row again,
        now carrying `erased_at`, so `EndUserIdentityService.exchange`'s
        guard has something to check against. Deleting the row would make
        that guard permanently unreachable and the next credential exchange
        would silently mint a new subject instead — the resurrection this
        finding exists to close. `profile` is the field design §3 names as
        where identifying detail actually lands, so it is what gets cleared;
        `external_user_id` and `channel` are the mapping itself, and have to
        survive for the refusal to have anything to refuse.

        `end_user_sessions` is revoked outright: an erased subject's own
        cookie must stop working in the same request that erases them, not
        keep authenticating until it expires on its own.
        """
        now = datetime.now(UTC)
        await self._session.execute(
            update(EndUserRow)
            .where(EndUserRow.id == end_user_id, EndUserRow.workspace_id == workspace_id)
            .values(erased_at=now)
        )
        await self._session.execute(
            update(ExternalIdentityRow)
            .where(
                ExternalIdentityRow.end_user_id == end_user_id,
                ExternalIdentityRow.workspace_id == workspace_id,
            )
            .values(profile=None)
        )
        await self._session.execute(
            update(EndUserSessionRow)
            .where(
                EndUserSessionRow.end_user_id == end_user_id,
                EndUserSessionRow.workspace_id == workspace_id,
                EndUserSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    async def _count(self, query: Select[tuple[int]]) -> int:
        value = await self._session.scalar(query)
        return int(value or 0)

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
        # who actually acted. For an end user erasing their own data,
        # `actor_id` is a key into `end_users`, not `users` — a hardcoded
        # "user" here pointed an auditor at the wrong table, or worse, a
        # different subject that happens to share the id. `SubjectService`
        # is now the one place that decides which type actually applies
        # (`_require_self_or_steward`'s own docstring), and this store
        # writes whatever it is told rather than re-deciding.
        self._session.add(
            AuditEventRow(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type="subject",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
                created_at=datetime.now(UTC),
            )
        )
        await self._session.flush()
