"""§1 and §3: five subjects resolve to five different reads, and reading
another workspace's trail as a platform administrator writes its own row.

One test per subject in §4.6's 审计记录 row, plus the two the plan calls out
by name as the easiest ways to get this wrong: a developer must not reach a
row about somebody else's resource (the negative test the plan requires),
and a same-workspace read must write nothing extra (the plan's own §3 exit
check, other direction).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tiny_hermes.audit.application.audit_service import (
    CROSS_WORKSPACE_READ,
    AuditService,
    ForbiddenAuditRead,
)
from tiny_hermes.audit.domain.query import filter_for
from tiny_hermes.audit.domain.record import AuditRecord
from tiny_hermes.audit.infrastructure.memory_store import MemoryAuditStore
from tiny_hermes.tenancy.domain.models import Actor, Role

WORKSPACE = uuid4()
OTHER_WORKSPACE = uuid4()


def _row(
    *,
    workspace_id: object = WORKSPACE,
    actor_id: object = None,
    resource_id: object = None,
    action: str = "agent.published",
    resource_type: str = "agent",
    context: dict[str, object] | None = None,
) -> AuditRecord:
    return AuditRecord(
        id=uuid4(),
        workspace_id=workspace_id,  # type: ignore[arg-type]
        actor_type="user",
        actor_id=actor_id or uuid4(),  # type: ignore[arg-type]
        action=action,
        resource_type=resource_type,
        resource_id=resource_id or uuid4(),  # type: ignore[arg-type]
        result="succeeded",
        request_id=str(uuid4()),
        context=context or {},
        created_at=datetime.now(UTC),
    )


# -- the five subjects --------------------------------------------------


async def test_workspace_admin_sees_everything_in_their_own_workspace() -> None:
    store = MemoryAuditStore()
    admin = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, admin.id, Role.WORKSPACE_ADMIN)
    row_a = store.seed(_row())
    row_b = store.seed(_row())
    service = AuditService(store)

    page = await service.list_events(admin, WORKSPACE, filter_for(), "req-1")

    assert {item.id for item in page.items} == {row_a.id, row_b.id}


async def test_platform_admin_with_no_membership_sees_everything_and_it_is_logged() -> None:
    """§4.6: 跨空间只读并留痕. No membership row at all is what makes this a
    cross-workspace read — see `AuditService.list_events`'s own docstring."""
    store = MemoryAuditStore()
    platform_admin = Actor.new(is_platform_admin=True)
    seeded = store.seed(_row())
    service = AuditService(store)

    page = await service.list_events(platform_admin, WORKSPACE, filter_for(), "req-2")

    # The read's own cross_workspace_read line is written *before* the
    # query runs (§3), so a full-visibility read of "everything" honestly
    # includes it alongside the row that was already there.
    assert seeded.id in {item.id for item in page.items}
    logged = store.rows_with_action(CROSS_WORKSPACE_READ)
    assert len(logged) == 1
    assert logged[0].actor_id == platform_admin.id
    assert logged[0].resource_id == WORKSPACE
    assert logged[0].workspace_id == WORKSPACE


async def test_platform_admin_who_is_also_a_member_reads_as_that_member_unlogged() -> None:
    """The `_require_member` precedent (`workspace_service.py`): an explicit
    membership row wins over the platform-admin bypass, so a platform admin
    who genuinely belongs to this workspace is not a "cross-workspace"
    visitor to it."""
    store = MemoryAuditStore()
    platform_admin = Actor.new(is_platform_admin=True)
    store.set_role(WORKSPACE, platform_admin.id, Role.WORKSPACE_ADMIN)
    store.seed(_row())
    service = AuditService(store)

    await service.list_events(platform_admin, WORKSPACE, filter_for(), "req-3")

    assert store.rows_with_action(CROSS_WORKSPACE_READ) == []


async def test_developer_sees_own_actions_and_rows_about_resources_they_touched() -> None:
    store = MemoryAuditStore()
    developer = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, developer.id, Role.DEVELOPER)
    resource = uuid4()
    own_action = store.seed(_row(actor_id=developer.id, resource_id=resource))
    #: A workspace admin later acted on the *same* resource — still visible,
    #: because it is about something this developer touched.
    followed_up = store.seed(_row(actor_id=uuid4(), resource_id=resource))
    #: Somebody else's resource entirely — this developer never appears on
    #: any row naming it, so it stays out of reach.
    store.seed(_row(actor_id=uuid4(), resource_id=uuid4()))
    service = AuditService(store)

    page = await service.list_events(developer, WORKSPACE, filter_for(), "req-4")

    assert {item.id for item in page.items} == {own_action.id, followed_up.id}


async def test_developer_cannot_see_a_row_about_someone_elses_resource() -> None:
    """The plan's own required negative test."""
    store = MemoryAuditStore()
    developer = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, developer.id, Role.DEVELOPER)
    unrelated = store.seed(_row(actor_id=uuid4(), resource_id=uuid4()))
    service = AuditService(store)

    page = await service.list_events(developer, WORKSPACE, filter_for(), "req-5")

    assert unrelated.id not in {item.id for item in page.items}


async def test_viewer_reads_context_stripped_of_every_unregistered_key() -> None:
    store = MemoryAuditStore()
    viewer = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, viewer.id, Role.VIEWER)
    store.seed(_row(context={"session_summary": "the customer is upset"}))
    service = AuditService(store)

    page = await service.list_events(viewer, WORKSPACE, filter_for(), "req-6")

    assert len(page.items) == 1
    assert page.items[0].context == {}
    assert "session_summary" not in str(page.items[0].context)


async def test_viewer_still_sees_every_row_only_context_is_touched() -> None:
    """脱敏只读, not 部分可见 by row — §2 redacts a column, not the row set."""
    store = MemoryAuditStore()
    viewer = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, viewer.id, Role.VIEWER)
    store.seed(_row())
    store.seed(_row())
    service = AuditService(store)

    page = await service.list_events(viewer, WORKSPACE, filter_for(), "req-7")

    assert len(page.items) == 2


async def test_end_user_actor_is_refused() -> None:
    """§4.6: 否. Defence in depth behind the HTTP-layer 403 — this service
    must refuse even if some future caller reaches it directly."""
    store = MemoryAuditStore()
    end_user = Actor(id=uuid4(), is_platform_admin=False, is_end_user=True)
    service = AuditService(store)

    with pytest.raises(ForbiddenAuditRead):
        await service.list_events(end_user, WORKSPACE, filter_for(), "req-8")


async def test_service_account_actor_is_refused() -> None:
    store = MemoryAuditStore()
    key = Actor(id=uuid4(), is_platform_admin=False, is_service_account=True)
    service = AuditService(store)

    with pytest.raises(ForbiddenAuditRead):
        await service.list_events(key, WORKSPACE, filter_for(), "req-9")


async def test_a_non_member_non_platform_admin_is_refused() -> None:
    store = MemoryAuditStore()
    stranger = Actor.new(is_platform_admin=False)
    service = AuditService(store)

    with pytest.raises(ForbiddenAuditRead):
        await service.list_events(stranger, WORKSPACE, filter_for(), "req-10")


# -- §3: leaving a trace only when it is a cross-workspace read ---------


async def test_same_workspace_read_writes_no_audit_row() -> None:
    store = MemoryAuditStore()
    admin = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, admin.id, Role.WORKSPACE_ADMIN)
    store.seed(_row())
    service = AuditService(store)

    await service.list_events(admin, WORKSPACE, filter_for(), "req-11")

    assert store.rows_with_action(CROSS_WORKSPACE_READ) == []


async def test_cross_workspace_audit_row_names_reader_target_and_filters() -> None:
    store = MemoryAuditStore()
    platform_admin = Actor.new(is_platform_admin=True)
    service = AuditService(store)

    await service.list_events(
        platform_admin,
        OTHER_WORKSPACE,
        filter_for(action="run.created"),
        "req-12",
    )

    logged = store.rows_with_action(CROSS_WORKSPACE_READ)
    assert len(logged) == 1
    assert logged[0].actor_id == platform_admin.id
    assert logged[0].resource_id == OTHER_WORKSPACE
    assert logged[0].context["action"] == "run.created"
    assert logged[0].created_at is not None


async def test_pagination_and_time_filters_apply() -> None:
    store = MemoryAuditStore()
    admin = Actor.new(is_platform_admin=False)
    store.set_role(WORKSPACE, admin.id, Role.WORKSPACE_ADMIN)
    for _ in range(3):
        store.seed(_row())
    service = AuditService(store)

    first_page = await service.list_events(
        admin, WORKSPACE, filter_for(limit=2), "req-13"
    )
    assert len(first_page.items) == 2
    assert first_page.has_more is True

    second_page = await service.list_events(
        admin, WORKSPACE, filter_for(limit=2, offset=2), "req-14"
    )
    assert len(second_page.items) == 1
    assert second_page.has_more is False
