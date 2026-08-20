"""Reviewing a candidate, and the roles §4.6 lets do it.

Three things are pinned. A candidate is decided once — a second decision is
refused rather than allowed to overwrite the first, the same stance §16.3 takes
on approvals. Only a workspace or platform administrator may act on somebody
else's private memory from the console, because that is the "代办并审计" row of
the matrix and nobody below it. And shared memory is written straight to
`active` by an administrator's edit, because that edit is itself the review §14.2
asks for.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tiny_hermes.memory.application.service import (
    ForbiddenMemoryAction,
    MemoryAlreadyDecided,
    MemoryRecord,
    MemoryService,
    UnknownAgent,
    UnknownMemory,
)
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryStatus
from tiny_hermes.tenancy.domain.models import Actor, Role

WORKSPACE = uuid4()
AGENT = uuid4()


def record(**overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": WORKSPACE,
        "agent_id": AGENT,
        "kind": MemoryKind.PRIVATE,
        "status": MemoryStatus.PENDING,
        "body": "Prefers the summary first.",
        "origin": "run",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


@dataclass
class FakeStore:
    roles: dict[UUID, Role] = field(default_factory=dict[UUID, Role])
    rows: dict[UUID, MemoryRecord] = field(default_factory=dict[UUID, MemoryRecord])
    audit: list[str] = field(default_factory=list[str])
    agents: set[UUID] = field(default_factory=lambda: {AGENT})

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        del workspace_id
        return self.roles.get(user_id)

    async def list_pending(self, workspace_id: UUID) -> Sequence[MemoryRecord]:
        return [
            r
            for r in self.rows.values()
            if r.workspace_id == workspace_id and r.status is MemoryStatus.PENDING
        ]

    async def get(self, memory_id: UUID) -> MemoryRecord | None:
        return self.rows.get(memory_id)

    async def set_status(
        self, memory_id: UUID, status: MemoryStatus, now: datetime
    ) -> MemoryRecord | None:
        row = self.rows[memory_id]
        updated = record(
            id=row.id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            kind=row.kind,
            status=status,
            body=row.body,
            origin=row.origin,
            created_at=row.created_at,
            updated_at=now,
        )
        self.rows[memory_id] = updated
        return updated

    async def create_shared(
        self, *, workspace_id: UUID, agent_id: UUID, body: str, created_by: UUID
    ) -> MemoryRecord:
        del created_by
        row = record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            kind=MemoryKind.SHARED,
            status=MemoryStatus.ACTIVE,
            body=body,
            origin="operator",
        )
        self.rows[row.id] = row
        return row

    async def agent_in_workspace(self, workspace_id: UUID, agent_id: UUID) -> bool:
        del workspace_id
        return agent_id in self.agents

    async def append_audit(self, *, action: str, **_: object) -> None:
        self.audit.append(action)


def service(store: FakeStore | None = None) -> tuple[MemoryService, FakeStore]:
    kept = store or FakeStore()
    return MemoryService(kept), kept


def admin() -> tuple[Actor, FakeStore]:
    who = Actor(uuid4(), False)
    store = FakeStore(roles={who.id: Role.WORKSPACE_ADMIN})
    return who, store


# -- who may review ----------------------------------------------------------


async def test_a_workspace_admin_approves_a_pending_candidate() -> None:
    who, store = admin()
    row = record()
    store.rows[row.id] = row
    catalog, _ = service(store)

    decided = await catalog.approve(who, WORKSPACE, row.id, "req-1")

    assert decided.status is MemoryStatus.ACTIVE
    assert store.audit == ["memory.active"]


async def test_a_developer_may_not_review() -> None:
    who = Actor(uuid4(), False)
    store = FakeStore(roles={who.id: Role.DEVELOPER})
    row = record()
    store.rows[row.id] = row
    catalog, _ = service(store)

    with pytest.raises(ForbiddenMemoryAction):
        await catalog.approve(who, WORKSPACE, row.id, "req-1")


async def test_a_service_account_may_not_review_even_as_admin() -> None:
    who = Actor(uuid4(), False, is_service_account=True, role=Role.WORKSPACE_ADMIN)
    catalog, store = service()
    row = record()
    store.rows[row.id] = row

    with pytest.raises(ForbiddenMemoryAction):
        await catalog.approve(who, WORKSPACE, row.id, "req-1")


async def test_a_platform_admin_may_review_without_a_membership() -> None:
    who = Actor(uuid4(), True)
    catalog, store = service()
    row = record()
    store.rows[row.id] = row

    decided = await catalog.approve(who, WORKSPACE, row.id, "req-1")

    assert decided.status is MemoryStatus.ACTIVE


# -- decided once ------------------------------------------------------------


async def test_a_rejected_candidate_is_kept_not_deleted() -> None:
    who, store = admin()
    row = record()
    store.rows[row.id] = row
    catalog, _ = service(store)

    decided = await catalog.reject(who, WORKSPACE, row.id, "req-1")

    assert decided.status is MemoryStatus.REJECTED
    assert store.rows[row.id].status is MemoryStatus.REJECTED


async def test_a_candidate_is_decided_once() -> None:
    who, store = admin()
    row = record(status=MemoryStatus.ACTIVE)
    store.rows[row.id] = row
    catalog, _ = service(store)

    with pytest.raises(MemoryAlreadyDecided):
        await catalog.approve(who, WORKSPACE, row.id, "req-1")


async def test_another_workspace_s_candidate_is_not_found() -> None:
    who, store = admin()
    row = record(workspace_id=uuid4())
    store.rows[row.id] = row
    catalog, _ = service(store)

    with pytest.raises(UnknownMemory):
        await catalog.approve(who, WORKSPACE, row.id, "req-1")


# -- shared memory's one door ------------------------------------------------


async def test_an_admin_edit_writes_shared_memory_active() -> None:
    who, store = admin()
    catalog, _ = service(store)

    created = await catalog.create_shared(
        who, WORKSPACE, agent_id=AGENT, body="The deploy window is Tuesdays.",
        request_id="req-1",
    )

    assert created.kind is MemoryKind.SHARED
    assert created.status is MemoryStatus.ACTIVE
    assert "memory.shared_created" in store.audit


async def test_shared_memory_for_an_agent_elsewhere_is_refused() -> None:
    who, store = admin()
    catalog, _ = service(store)

    with pytest.raises(UnknownAgent):
        await catalog.create_shared(
            who, WORKSPACE, agent_id=uuid4(), body="x", request_id="req-1"
        )


async def test_a_developer_may_not_edit_shared_memory() -> None:
    who = Actor(uuid4(), False)
    store = FakeStore(roles={who.id: Role.DEVELOPER})
    catalog, _ = service(store)

    with pytest.raises(ForbiddenMemoryAction):
        await catalog.create_shared(
            who, WORKSPACE, agent_id=AGENT, body="x", request_id="req-1"
        )
