"""What a subject may do with what this platform holds about them.

Product design §4.6's "本人会话与私有记忆的查看、更正、删除和导出" row, and the
erasure the roadmap's sixth exit criterion names. Four verbs and one procedure.

**A correction keeps the original.** §16.3 refuses to let a decision be
rewritten, and the same reasoning holds here: a corrected memory whose earlier
text is gone is a record nobody can audit, and "this was changed" and "this was
always so" would be indistinguishable afterwards. So a correction rejects the
old row and writes a new one, and both stay.

**Erasure is a deletion, not a flag.** After it runs, the subject's private
memories, sessions, messages and files are gone and not retrievable by any path
— which is what makes it worth doing. And because a deletion that left nothing
behind and one that never happened look identical afterwards, the erasure itself
writes an audit line. That line names the subject and the counts, never the
content: an audit trail that quoted what it deleted would be the copy the
deletion was for.

**Who may.** The subject themselves; a workspace administrator inside their
workspace, audited; a platform administrator, audited. §4.6 gives developers and
viewers nothing here, and that is asserted rather than assumed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.memory.application.service import MemoryRecord, cleaned_body
from tiny_hermes.memory.domain.scope import MemoryScope, MemoryStatus
from tiny_hermes.runs.domain.models import CallerIdentity
from tiny_hermes.tenancy.domain.models import Actor, Role

#: Who may act on somebody else's behalf, and only with an audit line.
STEWARDS = frozenset({Role.WORKSPACE_ADMIN})


@dataclass(frozen=True)
class ErasureReport:
    """What an erasure removed, in counts.

    Counts rather than content: this is what goes into the audit record, and an
    audit record holding what it deleted would be the copy the deletion was
    meant to remove.
    """

    memories: int
    sessions: int
    messages: int
    artifacts: int


@dataclass(frozen=True)
class SubjectExport:
    """Everything this platform holds about one subject in one workspace."""

    subject: CallerIdentity
    workspace_id: UUID
    memories: Sequence[MemoryRecord]
    sessions: Sequence[UUID]


class SubjectStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def memories_of(
        self, scope: MemoryScope
    ) -> Sequence[MemoryRecord]: ...

    async def memories_of_subject(
        self, workspace_id: UUID, subject: CallerIdentity
    ) -> Sequence[MemoryRecord]:
        """Every private memory this subject has, under any Agent.

        `memories_of` needs a scope, and a scope needs one Agent (`MemoryScope`'s
        own docstring: "no way to express every subject's memory" — but that
        refusal is about widening *across subjects*, not about one subject's
        own export reaching past whichever single Agent a caller happened to
        name). §348's export is the subject's data, not one Agent's slice of
        it — this is what `export` calls when its caller passed no `agent_id`,
        which is what every real caller does; nothing in this codebase has an
        Agent picker for a subject to choose from.
        """
        ...

    async def get(self, memory_id: UUID) -> MemoryRecord | None: ...

    async def subject_of(self, memory_id: UUID) -> CallerIdentity | None:
        """Whose memory this is, or `None` for a shared one.

        Asked before a subject is allowed to touch a row, so "my own" is a
        check against the row rather than a claim in the request.
        """
        ...

    async def replace(
        self, memory_id: UUID, body: str, now: datetime
    ) -> MemoryRecord: ...

    async def set_status(
        self, memory_id: UUID, status: MemoryStatus, now: datetime
    ) -> MemoryRecord | None: ...

    async def sessions_of(
        self, workspace_id: UUID, subject: CallerIdentity
    ) -> Sequence[UUID]: ...

    async def erase(
        self, workspace_id: UUID, subject: CallerIdentity
    ) -> ErasureReport: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class SubjectError(Exception):
    """Base for every expected refusal here."""


class ForbiddenSubjectAction(SubjectError):
    pass


class UnknownSubjectMemory(SubjectError):
    pass


@dataclass(frozen=True)
class SubjectService:
    store: SubjectStore

    async def export(
        self,
        actor: Actor,
        workspace_id: UUID,
        subject: CallerIdentity,
        agent_id: UUID | None,
        request_id: str,
    ) -> SubjectExport:
        """Everything held about this subject, for them to take away.

        `agent_id` narrows to one Agent's memory when a caller names one — the
        console's own export route accepts it for exactly that reason. `None`
        is not "no memory" (it used to return that, review finding 3); it is
        "every Agent this subject has used", which is what the door's only
        real caller — `apps/chat-web`'s settings page, no Agent picker in
        sight — always asks for, and the only sensible default for "everything
        held about this subject" to begin with.

        An already-erased subject exports empty rather than failing: "there is
        nothing" is the honest answer, and an error would read as "something
        went wrong" to somebody who had just asked for their data to be gone.
        """
        await self._require_self_or_steward(actor, workspace_id, subject, request_id)
        if agent_id is not None:
            memories = list(
                await self.store.memories_of(
                    MemoryScope.private(
                        workspace_id=workspace_id, agent_id=agent_id, subject=subject
                    )
                )
            )
        else:
            memories = list(await self.store.memories_of_subject(workspace_id, subject))
        return SubjectExport(
            subject=subject,
            workspace_id=workspace_id,
            memories=memories,
            sessions=list(await self.store.sessions_of(workspace_id, subject)),
        )

    async def correct(
        self,
        actor: Actor,
        workspace_id: UUID,
        memory_id: UUID,
        body: str,
        request_id: str,
    ) -> MemoryRecord:
        """Replace what a memory says, keeping what it said.

        The old row is rejected rather than overwritten — see the module
        docstring. What comes back is the new one.
        """
        owner = await self._owned(actor, workspace_id, memory_id, request_id)
        del owner
        corrected = await self.store.replace(memory_id, cleaned_body(body), _now())
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="memory.corrected",
            resource_id=memory_id,
            request_id=request_id,
        )
        return corrected

    async def forget(
        self, actor: Actor, workspace_id: UUID, memory_id: UUID, request_id: str
    ) -> MemoryRecord:
        """Take one memory out of use, on the subject's say-so."""
        await self._owned(actor, workspace_id, memory_id, request_id)
        removed = await self.store.set_status(
            memory_id, MemoryStatus.REJECTED, _now()
        )
        if removed is None:  # pragma: no cover - read a line above
            raise UnknownSubjectMemory
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="memory.forgotten",
            resource_id=memory_id,
            request_id=request_id,
        )
        return removed

    async def erase(
        self,
        actor: Actor,
        workspace_id: UUID,
        subject: CallerIdentity,
        request_id: str,
    ) -> ErasureReport:
        """Remove everything this platform holds about a subject here.

        The audit line is written **after** the deletion and carries counts
        only. It is the whole reason an erasure is distinguishable from an
        erasure that never happened.
        """
        await self._require_self_or_steward(actor, workspace_id, subject, request_id)
        report = await self.store.erase(workspace_id, subject)
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="subject.erased",
            resource_id=subject.caller_id,
            request_id=request_id,
            context={
                "subject_type": subject.caller_type.value,
                "memories": str(report.memories),
                "sessions": str(report.sessions),
                "messages": str(report.messages),
                "artifacts": str(report.artifacts),
            },
        )
        return report

    async def _owned(
        self, actor: Actor, workspace_id: UUID, memory_id: UUID, request_id: str
    ) -> CallerIdentity:
        record = await self.store.get(memory_id)
        if record is None or record.workspace_id != workspace_id:
            raise UnknownSubjectMemory
        owner = await self.store.subject_of(memory_id)
        if owner is None:
            # Shared memory is the Agent's, not anybody's. §14.2 gives it two
            # doors and neither of them is a subject correcting their own.
            raise ForbiddenSubjectAction
        await self._require_self_or_steward(actor, workspace_id, owner, request_id)
        return owner

    async def _require_self_or_steward(
        self,
        actor: Actor,
        workspace_id: UUID,
        subject: CallerIdentity,
        request_id: str,
    ) -> None:
        if actor.is_service_account:
            # §4.6 gives this row to people. A key acting on somebody's private
            # data is what the row exists to prevent.
            raise ForbiddenSubjectAction
        if actor.id == subject.caller_id:
            return
        role = await self.store.user_role(workspace_id, actor.id)
        if role in STEWARDS or actor.is_platform_admin:
            # Acting for somebody else is allowed and recorded. The action's own
            # audit line follows; this one says whose behalf it was on.
            await self.store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="subject.acted_on_behalf",
                resource_id=subject.caller_id,
                request_id=request_id,
            )
            return
        raise ForbiddenSubjectAction


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
