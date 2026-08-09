import pytest
from tiny_hermes.tenancy.application.workspace_service import Forbidden, WorkspaceService
from tiny_hermes.tenancy.domain.models import Actor, Role
from tiny_hermes.tenancy.infrastructure.memory_store import MemoryWorkspaceStore


async def test_only_platform_admin_can_create_workspace() -> None:
    service = WorkspaceService(MemoryWorkspaceStore())
    ordinary = Actor.new(is_platform_admin=False)

    with pytest.raises(Forbidden):
        await service.create_workspace(ordinary, "Acme", "req-1")


async def test_creator_becomes_workspace_admin() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)

    workspace = await service.create_workspace(admin, "Acme", "req-2")

    assert store.role_for(workspace.id, admin.id) is Role.WORKSPACE_ADMIN
    assert store.audit_actions == ["workspace.created"]


async def test_member_lists_own_workspace_but_not_other_members() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)
    member = Actor.new(is_platform_admin=False)
    outsider = Actor.new(is_platform_admin=False)
    workspace = await service.create_workspace(admin, "Acme", "req-1")
    await store.add_membership(workspace.id, member.id, Role.VIEWER)

    assert await service.list_workspaces(member) == [workspace]
    assert len(await service.list_members(member, workspace.id, "req-2")) == 2
    with pytest.raises(Forbidden):
        await service.list_members(outsider, workspace.id, "req-3")


async def test_platform_admin_cross_workspace_member_read_is_audited() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    owner = Actor.new(is_platform_admin=False)
    platform_admin = Actor.new(is_platform_admin=True)
    workspace = await store.create_workspace("Acme")
    await store.add_membership(workspace.id, owner.id, Role.WORKSPACE_ADMIN)

    await service.list_members(platform_admin, workspace.id, "req-read")

    assert store.audit_actions == ["workspace.members_read_by_platform_admin"]
