"""Reviewing memory candidates, and the one door shared memory has here.

Product design §14.1 and §14.2. Two jobs, and the rules about *who* are §4.6's
matrix applied in one place.

**A candidate is reviewed, not a Run resumed.** Unlike a governance approval, a
pending memory holds up nothing — the Run that proposed it has moved on. So
this is not the M2C approval flow with a different noun; it is its own small
thing, and the `pending` row is its own queue. Approving flips it to `active`;
rejecting flips it to `rejected` and keeps it, so the same candidate proposed
twice is visible as such.

**Shared memory has exactly one door here.** §14.2 gives it two ways in — an
administrator's direct edit and an approved proposal — and this module is the
first. A running Agent is neither, which is why `SqlMemoryCandidates` writes
only private rows and `create_shared` is the only way a `kind=shared` row is
born from a person.

**Who may act is §4.6, not a habit.** Private memory is the subject's own;
inside the console a workspace or platform administrator acts on their behalf
and it is audited. A developer and a viewer may not, and the matrix says so.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.memory.domain.policy import MAX_BODY_LENGTH
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryStatus
from tiny_hermes.tenancy.domain.models import Actor, Role

#: Who may act on somebody else's private memory from inside the console, and
#: who may edit shared memory. §4.6's two "代办并审计" rows and the governance
#: row, which are the same two roles.
STEWARDS = frozenset({Role.WORKSPACE_ADMIN})


@dataclass(frozen=True)
class MemoryRecord:
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    kind: MemoryKind
    status: MemoryStatus
    body: str
    origin: str
    created_at: datetime
    updated_at: datetime


class MemoryStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def list_pending(self, workspace_id: UUID) -> Sequence[MemoryRecord]: ...

    async def get(self, memory_id: UUID) -> MemoryRecord | None: ...

    async def set_status(
        self, memory_id: UUID, status: MemoryStatus, now: datetime
    ) -> MemoryRecord | None: ...

    async def create_shared(
        self, *, workspace_id: UUID, agent_id: UUID, body: str, created_by: UUID
    ) -> MemoryRecord: ...

    async def agent_in_workspace(self, workspace_id: UUID, agent_id: UUID) -> bool: ...

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


class MemoryError(Exception):
    """Base for every expected refusal here."""


class ForbiddenMemoryAction(MemoryError):
    pass


class UnknownMemory(MemoryError):
    pass


class UnknownAgent(MemoryError):
    pass


class MemoryAlreadyDecided(MemoryError):
    def __init__(self, status: MemoryStatus) -> None:
        super().__init__(f"this candidate was already {status.value}")
        self.status = status


class InvalidMemoryBody(MemoryError):
    pass


@dataclass(frozen=True)
class MemoryService:
    store: MemoryStore

    async def list_pending(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> Sequence[MemoryRecord]:
        del request_id
        await self._require_steward(actor, workspace_id)
        return await self.store.list_pending(workspace_id)

    async def approve(
        self, actor: Actor, workspace_id: UUID, memory_id: UUID, request_id: str
    ) -> MemoryRecord:
        return await self._decide(
            actor, workspace_id, memory_id, MemoryStatus.ACTIVE, request_id
        )

    async def reject(
        self, actor: Actor, workspace_id: UUID, memory_id: UUID, request_id: str
    ) -> MemoryRecord:
        return await self._decide(
            actor, workspace_id, memory_id, MemoryStatus.REJECTED, request_id
        )

    async def create_shared(
        self,
        actor: Actor,
        workspace_id: UUID,
        *,
        agent_id: UUID,
        body: str,
        request_id: str,
    ) -> MemoryRecord:
        """§14.2's first door. Written `active` straight away, because an
        administrator's own edit is the review — there is nobody else it is
        waiting on."""
        await self._require_steward(actor, workspace_id)
        cleaned = _cleaned(body)
        if not await self.store.agent_in_workspace(workspace_id, agent_id):
            raise UnknownAgent
        record = await self.store.create_shared(
            workspace_id=workspace_id,
            agent_id=agent_id,
            body=cleaned,
            created_by=actor.id,
        )
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="memory.shared_created",
            resource_id=record.id,
            request_id=request_id,
            context={"agent_id": str(agent_id)},
        )
        return record

    async def _decide(
        self,
        actor: Actor,
        workspace_id: UUID,
        memory_id: UUID,
        status: MemoryStatus,
        request_id: str,
    ) -> MemoryRecord:
        await self._require_steward(actor, workspace_id)
        record = await self.store.get(memory_id)
        if record is None or record.workspace_id != workspace_id:
            # Another workspace's memory is not found here, the answer every
            # other catalog gives and for the same reason.
            raise UnknownMemory
        if record.status is not MemoryStatus.PENDING:
            raise MemoryAlreadyDecided(record.status)
        decided = await self.store.set_status(
            memory_id, status, datetime_now()
        )
        if decided is None:  # pragma: no cover - read a line above
            raise UnknownMemory
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=f"memory.{status.value}",
            resource_id=memory_id,
            request_id=request_id,
            context={"kind": record.kind.value},
        )
        return decided

    async def _require_steward(self, actor: Actor, workspace_id: UUID) -> None:
        if actor.is_service_account:
            # A key does not review somebody's memory. §4.6 gives this to
            # people, and a machine acting on a person's private data is the
            # thing the row exists to prevent.
            raise ForbiddenMemoryAction
        role = await self.store.user_role(workspace_id, actor.id)
        if role in STEWARDS:
            return
        if not actor.is_platform_admin:
            raise ForbiddenMemoryAction


def _cleaned(body: str) -> str:
    text = body.strip()
    if not text:
        raise InvalidMemoryBody("a memory has a body")
    if len(text) > MAX_BODY_LENGTH:
        raise InvalidMemoryBody(f"a memory is at most {MAX_BODY_LENGTH} characters")
    return text


def datetime_now() -> datetime:
    """Wrapped so a test can hold the clock still; the store stamps rows with
    the same instant it decides them."""
    from datetime import UTC

    return datetime.now(UTC)
