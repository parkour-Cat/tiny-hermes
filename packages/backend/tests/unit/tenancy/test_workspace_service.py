import pytest
from tiny_hermes.tenancy.application.workspace_service import (
    Forbidden,
    LastWorkspaceAdmin,
    MemberAlreadyPresent,
    UnknownUser,
    WorkspaceService,
)
from tiny_hermes.tenancy.domain.models import Actor, Role, WorkspaceMember
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


async def test_admin_invites_an_existing_user_by_email() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)
    invitee_id = Actor.new(is_platform_admin=False).id
    store.register_person(invitee_id, "Dev", "dev@example.com")
    workspace = await service.create_workspace(admin, "Acme", "req-1")

    member = await service.invite_member(
        admin, workspace.id, "dev@example.com", Role.DEVELOPER, "req-2"
    )

    assert member.user_id == invitee_id
    assert member.subject == "dev@example.com"
    assert member.role is Role.DEVELOPER
    assert store.role_for(workspace.id, invitee_id) is Role.DEVELOPER
    assert "workspace.member_invited" in store.audit_actions[-1]


async def test_unknown_email_is_not_an_implicit_signup() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)
    workspace = await service.create_workspace(admin, "Acme", "req-1")

    with pytest.raises(UnknownUser):
        await service.invite_member(
            admin, workspace.id, "missing@example.com", Role.VIEWER, "req-2"
        )
    assert await service.list_members(admin, workspace.id, "req-3") == [
        WorkspaceMember(admin.id, str(admin.id), "", Role.WORKSPACE_ADMIN)
    ]


async def test_duplicate_invite_is_refused() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)
    invitee_id = Actor.new(is_platform_admin=False).id
    store.register_person(invitee_id, "Dev", "dev@example.com")
    workspace = await service.create_workspace(admin, "Acme", "req-1")
    await service.invite_member(
        admin, workspace.id, "dev@example.com", Role.DEVELOPER, "req-2"
    )

    with pytest.raises(MemberAlreadyPresent):
        await service.invite_member(
            admin, workspace.id, "dev@example.com", Role.VIEWER, "req-3"
        )


async def test_developer_cannot_invite() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)
    developer = Actor.new(is_platform_admin=False)
    invitee_id = Actor.new(is_platform_admin=False).id
    store.register_person(invitee_id, "Other", "other@example.com")
    workspace = await service.create_workspace(admin, "Acme", "req-1")
    await store.add_membership(workspace.id, developer.id, Role.DEVELOPER)

    with pytest.raises(Forbidden):
        await service.invite_member(
            developer, workspace.id, "other@example.com", Role.VIEWER, "req-2"
        )


async def test_the_last_workspace_admin_cannot_be_removed_or_demoted() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)
    workspace = await service.create_workspace(admin, "Acme", "req-1")

    with pytest.raises(LastWorkspaceAdmin):
        await service.remove_member(admin, workspace.id, admin.id, "req-2")
    with pytest.raises(LastWorkspaceAdmin):
        await service.change_member_role(
            admin, workspace.id, admin.id, Role.DEVELOPER, "req-3"
        )
    assert store.role_for(workspace.id, admin.id) is Role.WORKSPACE_ADMIN
