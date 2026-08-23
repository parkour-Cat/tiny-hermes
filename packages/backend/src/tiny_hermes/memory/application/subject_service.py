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
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType
from tiny_hermes.tenancy.domain.models import Actor, Role

#: Who may act on somebody else's behalf, and only with an audit line.
STEWARDS = frozenset({Role.WORKSPACE_ADMIN})


@dataclass(frozen=True)
class ResolvedSubject:
    """One subject, found by the name an enterprise's own directory uses.

    `erased_at` is carried because §344 keeps the row after an erasure —
    Runs reference it, and the fact that somebody was here is not what an
    erasure removes. A second request from the same person needs "already
    erased, on this date", which is a different answer from "no such
    person" and the only true one.
    """

    subject_id: UUID
    channel: str
    external_user_id: str
    erased_at: datetime | None
    first_seen_at: datetime


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

    async def resolve_external(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> "ResolvedSubject | None": ...

    async def subject_type_of(
        self, workspace_id: UUID, subject_id: UUID
    ) -> CallerType | None:
        """Which kind of subject this id is, or `None` for neither.

        `users` and `end_users` are separate id spaces, so an id belongs to
        at most one of them and the answer is not a guess.
        """
        ...

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
        # Task-9 review finding C: no default. A caller that forgets to pass
        # this is a caller that has not decided who acted, and the fix for
        # that is a type error here, not a silent "user" the way the old
        # hardcoded value was.
        actor_type: str,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class SubjectError(Exception):
    """Base for every expected refusal here."""


class ForbiddenSubjectAction(SubjectError):
    pass


class UnknownSubject(SubjectError):
    """Nobody here goes by that name on that channel.

    One exception for "no such identity" and for "not in this workspace":
    telling them apart would let a steward of one tenant confirm that a
    named person is an end user of another.
    """


class UnknownSubjectMemory(SubjectError):
    pass


@dataclass(frozen=True)
class SubjectService:
    store: SubjectStore

    async def subject_in(
        self, workspace_id: UUID, subject_id: UUID
    ) -> CallerIdentity:
        """The subject an id stands for, read rather than assumed.

        `subject_routes.py` used to build `CallerType.USER` for every id it
        was handed, defending it on the grounds that the router is
        console-only and an end user never calls it. That is true of the
        *caller* and says nothing about whose data they are acting on —
        which for §4.6's `代办` row is exactly the question.

        The cost was as bad as this feature gets: erasing an end user
        deleted rows matching `caller_type='user'`, found none, and
        answered `200` with a report of zeros while everything stayed. An
        erasure that says it happened and did not is worse than one that
        fails.
        """
        found = await self.store.subject_type_of(workspace_id, subject_id)
        if found is None:
            # Refused rather than assumed. "Nothing was held about them" and
            # "that id is nobody" are different answers, and an
            # administrator replying to a request needs to know which.
            raise UnknownSubject
        return CallerIdentity(caller_type=found, caller_id=subject_id)

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
        _owner, actor_type = await self._owned(actor, workspace_id, memory_id, request_id)
        corrected = await self.store.replace(memory_id, cleaned_body(body), _now())
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            actor_type=actor_type.value,
            action="memory.corrected",
            resource_id=memory_id,
            request_id=request_id,
        )
        return corrected

    async def forget(
        self, actor: Actor, workspace_id: UUID, memory_id: UUID, request_id: str
    ) -> MemoryRecord:
        """Take one memory out of use, on the subject's say-so."""
        _owner, actor_type = await self._owned(actor, workspace_id, memory_id, request_id)
        removed = await self.store.set_status(
            memory_id, MemoryStatus.REJECTED, _now()
        )
        if removed is None:  # pragma: no cover - read a line above
            raise UnknownSubjectMemory
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            actor_type=actor_type.value,
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
        actor_type = await self._require_self_or_steward(
            actor, workspace_id, subject, request_id
        )
        report = await self.store.erase(workspace_id, subject)
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            actor_type=actor_type.value,
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

    async def lookup(
        self,
        actor: Actor,
        workspace_id: UUID,
        channel: str,
        external_user_id: str,
        request_id: str,
    ) -> ResolvedSubject:
        """The subject a data-rights request names, by their external id.

        Steward-only, with no "self" branch: an end user asking about their
        own data goes through `end_user_subject_routes.py`, which reads the
        subject from their credential and never needs to resolve a name. A
        self branch here would be a second way to reach that, gated on a
        string the caller supplies.

        Audited on every call, hit or miss. Resolving a name is the first
        step of acting on somebody's data and it is worth a line whether or
        not the person turned out to exist — an audit that only recorded
        successful lookups would leave "who did we search for" unanswerable.
        """
        if actor.is_service_account:
            raise ForbiddenSubjectAction
        role = await self.store.user_role(workspace_id, actor.id)
        if role not in STEWARDS and not actor.is_platform_admin:
            raise ForbiddenSubjectAction
        found = await self.store.resolve_external(workspace_id, channel, external_user_id)
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            actor_type=CallerType.USER.value,
            action="subject.looked_up",
            resource_id=found.subject_id if found else workspace_id,
            request_id=request_id,
            context={"channel": channel, "found": "true" if found else "false"},
        )
        if found is None:
            raise UnknownSubject
        return found

    async def _owned(
        self, actor: Actor, workspace_id: UUID, memory_id: UUID, request_id: str
    ) -> tuple[CallerIdentity, CallerType]:
        record = await self.store.get(memory_id)
        if record is None or record.workspace_id != workspace_id:
            raise UnknownSubjectMemory
        owner = await self.store.subject_of(memory_id)
        if owner is None:
            # Shared memory is the Agent's, not anybody's. §14.2 gives it two
            # doors and neither of them is a subject correcting their own.
            raise ForbiddenSubjectAction
        actor_type = await self._require_self_or_steward(actor, workspace_id, owner, request_id)
        return owner, actor_type

    async def _require_self_or_steward(
        self,
        actor: Actor,
        workspace_id: UUID,
        subject: CallerIdentity,
        request_id: str,
    ) -> CallerType:
        """Approves the action and returns the type the caller actually
        acted as — `end_user` or `user` — for the action's own audit line to
        carry (task-9 review finding C).

        Task-9 review finding D: "self" used to mean `actor.id ==
        subject.caller_id` alone, a bare id comparison that happens to be
        safe today only because `end_user_subject_routes.py` never hands
        this an end-user-typed `Actor` next to a `CallerType.USER` subject
        or vice versa — not because the comparison itself rules that out.
        `actor.is_end_user` (mirroring `is_service_account`, both facts
        about which kind of subject an `Actor` stands in for) is compared
        alongside the id now, so agreement on type is required, not assumed.
        A steward can never take the self branch by construction: an end
        user's id never resolves a workspace `Role` (`end_users` and
        `memberships.user_id` are different namespaces), so `role` below is
        always `None` for one, and `actor.is_platform_admin` is always
        `False` — the steward branch stays exactly as unreachable for an end
        user as it always should have been, now for a structural reason
        instead of a coincidental one.
        """
        if actor.is_service_account:
            # §4.6 gives this row to people. A key acting on somebody's private
            # data is what the row exists to prevent.
            raise ForbiddenSubjectAction
        caller_type = CallerType.END_USER if actor.is_end_user else CallerType.USER
        if caller_type is subject.caller_type and actor.id == subject.caller_id:
            return caller_type
        role = await self.store.user_role(workspace_id, actor.id)
        if role in STEWARDS or actor.is_platform_admin:
            # Acting for somebody else is allowed and recorded. The action's own
            # audit line follows; this one says whose behalf it was on. Always
            # `caller_type` here too: reaching this branch already proved the
            # actor is not the subject, and a steward is always a real
            # console user (see the docstring above), so this and the
            # action's own line agree without needing a second computation.
            await self.store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                actor_type=caller_type.value,
                action="subject.acted_on_behalf",
                resource_id=subject.caller_id,
                request_id=request_id,
            )
            return caller_type
        raise ForbiddenSubjectAction


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
