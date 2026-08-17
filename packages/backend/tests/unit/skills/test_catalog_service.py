"""Who the catalog lets act, and what it refuses to store.

The store tests next door say what the rows are. These say who is allowed to
make them, which is the part a route could get wrong without any test failing:
`MemorySkillStore` will happily write whatever it is asked to.

Product design §15.1 for the two levels, §9.3 for workspace scope.
"""

from uuid import UUID, uuid4

import pytest
from tiny_hermes.skills.application.service import (
    ForbiddenSkillAction,
    InvalidSkillPackage,
    SkillCatalog,
    SkillNameMismatch,
    SkillNameTaken,
    SkillScanRefused,
    UnknownSkill,
    VersionNotBindable,
)
from tiny_hermes.skills.domain.models import Skill, SkillScope, SkillVersionStatus
from tiny_hermes.skills.domain.package import SkillFile
from tiny_hermes.skills.infrastructure.memory_store import MemorySkillStore
from tiny_hermes.tenancy.domain.models import Actor, Role

REQUEST = "request-1"

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes

Read [the house style](style.md) and write one paragraph per kind of change.
"""


def files(body: str = "Short sentences.") -> tuple[SkillFile, ...]:
    return (
        SkillFile(path="SKILL.md", text=SKILL_MD),
        SkillFile(path="style.md", text=body),
    )


@pytest.fixture
def store() -> MemorySkillStore:
    return MemorySkillStore()


@pytest.fixture
def catalog(store: MemorySkillStore) -> SkillCatalog:
    return SkillCatalog(store)


@pytest.fixture
def workspace_id() -> UUID:
    return uuid4()


def member(store: MemorySkillStore, workspace_id: UUID, role: Role) -> Actor:
    actor = Actor(uuid4(), is_platform_admin=False)
    store.memberships[(workspace_id, actor.id)] = role
    return actor


def platform_admin() -> Actor:
    return Actor(uuid4(), is_platform_admin=True)


async def create(
    catalog: SkillCatalog,
    actor: Actor,
    workspace_id: UUID,
    *,
    scope: SkillScope = SkillScope.WORKSPACE,
    body: str = "Short sentences.",
) -> Skill:
    skill, _ = await catalog.create_skill(
        actor, workspace_id, scope, files(body), REQUEST
    )
    return skill


async def test_a_developer_may_create_a_workspace_skill(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill = await create(catalog, actor, workspace_id)
    assert skill.name == "release-notes"
    assert skill.scope is SkillScope.WORKSPACE
    assert skill.workspace_id == workspace_id
    # The first version is where new bindings start, without a second request.
    assert skill.current_version_id is not None


async def test_a_viewer_may_read_and_may_not_write(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.WORKSPACE_ADMIN)
    await create(catalog, author, workspace_id)
    viewer = member(store, workspace_id, Role.VIEWER)
    assert len(await catalog.list_skills(viewer, workspace_id, REQUEST)) == 1
    with pytest.raises(ForbiddenSkillAction):
        await create(catalog, viewer, workspace_id)


async def test_a_stranger_to_the_workspace_may_not_even_list(
    catalog: SkillCatalog, workspace_id: UUID
) -> None:
    with pytest.raises(ForbiddenSkillAction):
        await catalog.list_skills(
            Actor(uuid4(), is_platform_admin=False), workspace_id, REQUEST
        )


async def test_a_service_account_may_not_write_a_skill(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """Skills are authored by people.

    A key that could rewrite what every Run is told would be a quiet way past
    §7's approval, so it is refused before its role is even consulted.
    """
    key = Actor(
        uuid4(),
        is_platform_admin=False,
        is_service_account=True,
        role=Role.WORKSPACE_ADMIN,
    )
    with pytest.raises(ForbiddenSkillAction):
        await create(catalog, key, workspace_id)
    del store


async def test_one_workspace_cannot_read_another_workspace_s_skill(
    catalog: SkillCatalog, store: MemorySkillStore
) -> None:
    """The refusal is `UnknownSkill`, not `Forbidden`: which skills exist
    elsewhere is exactly what an outsider is not entitled to learn."""
    mine, theirs = uuid4(), uuid4()
    author = member(store, theirs, Role.DEVELOPER)
    skill = await create(catalog, author, theirs)
    outsider = member(store, mine, Role.WORKSPACE_ADMIN)
    with pytest.raises(UnknownSkill):
        await catalog.get_skill(outsider, mine, skill.id, REQUEST)
    assert await catalog.list_skills(outsider, mine, REQUEST) == []


async def test_a_platform_skill_is_readable_from_a_workspace_and_not_writable(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """§15.1's built-in skills, from the far side of §9.3's scope rule."""
    skill = await create(
        catalog, platform_admin(), workspace_id, scope=SkillScope.PLATFORM
    )
    assert skill.workspace_id is None
    admin = member(store, workspace_id, Role.WORKSPACE_ADMIN)
    assert [item.id for item in await catalog.list_skills(admin, workspace_id, REQUEST)] == [
        skill.id
    ]
    assert (await catalog.get_skill(admin, workspace_id, skill.id, REQUEST)).id == skill.id
    with pytest.raises(ForbiddenSkillAction):
        await catalog.upload_version(
            admin, workspace_id, skill.id, files("Shorter sentences."), REQUEST
        )


async def test_a_workspace_admin_may_not_create_a_platform_skill(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    admin = member(store, workspace_id, Role.WORKSPACE_ADMIN)
    with pytest.raises(ForbiddenSkillAction):
        await create(catalog, admin, workspace_id, scope=SkillScope.PLATFORM)


async def test_the_same_files_uploaded_twice_do_not_publish_again(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """The roadmap's exit check. The route turns `created` into 200 or 201."""
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill = await create(catalog, actor, workspace_id)
    again = await catalog.upload_version(actor, workspace_id, skill.id, files(), REQUEST)
    assert again.created is False
    assert again.version.id == skill.current_version_id
    assert len(await catalog.list_versions(actor, workspace_id, skill.id, REQUEST)) == 1


async def test_changed_files_publish_a_second_version(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill = await create(catalog, actor, workspace_id)
    second = await catalog.upload_version(
        actor, workspace_id, skill.id, files("Shorter sentences."), REQUEST
    )
    assert second.created is True
    assert second.version.version_number == 2
    # Publishing does not move the default. Somebody decides that separately.
    unchanged = await catalog.get_skill(actor, workspace_id, skill.id, REQUEST)
    assert unchanged.current_version_id != second.version.id


async def test_an_upload_carrying_credentials_is_refused_and_stores_nothing(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """Red line four: the scan runs before a row exists, not after."""
    actor = member(store, workspace_id, Role.DEVELOPER)
    leaked = "Use AKIAIOSFODNN7EXAMPLE when you call the API.\n"
    with pytest.raises(SkillScanRefused) as refusal:
        await create(catalog, actor, workspace_id, body=leaked)
    assert [finding.code for finding in refusal.value.findings] == ["credential_material"]
    assert store.skills == {}
    assert store.versions == {}


async def test_an_advisory_finding_still_publishes(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """Refusing everything the scan notices teaches people to dodge the scan."""
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill, _ = await catalog.create_skill(
        actor,
        workspace_id,
        SkillScope.WORKSPACE,
        (*files(), SkillFile(path="fetch.sh", text="curl https://example.com\n")),
        REQUEST,
    )
    versions = await catalog.list_versions(actor, workspace_id, skill.id, REQUEST)
    assert versions[0].bindable
    assert "network_in_script" in {finding.code for finding in versions[0].findings}


async def test_files_that_are_not_a_package_are_refused_by_shape(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    actor = member(store, workspace_id, Role.DEVELOPER)
    with pytest.raises(InvalidSkillPackage):
        await catalog.create_skill(
            actor,
            workspace_id,
            SkillScope.WORKSPACE,
            (SkillFile(path="style.md", text="No manifest here."),),
            REQUEST,
        )


async def test_a_name_is_taken_once_per_level(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    actor = member(store, workspace_id, Role.DEVELOPER)
    await create(catalog, actor, workspace_id)
    with pytest.raises(SkillNameTaken):
        await create(catalog, actor, workspace_id, body="Shorter sentences.")


async def test_an_upload_may_not_rename_the_skill_it_lands_in(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """A name is what an AgentSpec binds and what §6 shows the model. Letting
    an upload move it would change bindings nobody touched."""
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill = await create(catalog, actor, workspace_id)
    renamed = SKILL_MD.replace("name: release-notes", "name: changelog")
    with pytest.raises(SkillNameMismatch):
        await catalog.upload_version(
            actor,
            workspace_id,
            skill.id,
            (SkillFile(path="SKILL.md", text=renamed), SkillFile(path="style.md", text="x")),
            REQUEST,
        )


async def test_withdrawing_the_default_version_clears_the_default(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """Otherwise the default points at something no new binding may name, and
    the next create fails with a message about the wrong thing."""
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill = await create(catalog, actor, workspace_id)
    assert skill.current_version_id is not None
    withdrawn = await catalog.withdraw_version(
        actor, workspace_id, skill.id, skill.current_version_id, REQUEST
    )
    assert withdrawn.status is SkillVersionStatus.WITHDRAWN
    after = await catalog.get_skill(actor, workspace_id, skill.id, REQUEST)
    assert after.current_version_id is None
    # Still readable: a Run bound to it has to keep working.
    _, bodies = await catalog.read_version(
        actor, workspace_id, skill.id, withdrawn.id, REQUEST
    )
    assert len(bodies) == 2


async def test_the_default_may_be_rolled_back_but_not_to_a_withdrawn_version(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    actor = member(store, workspace_id, Role.DEVELOPER)
    skill = await create(catalog, actor, workspace_id)
    first = skill.current_version_id
    assert first is not None
    second = await catalog.upload_version(
        actor, workspace_id, skill.id, files("Shorter sentences."), REQUEST
    )
    moved = await catalog.set_current_version(
        actor, workspace_id, skill.id, second.version.id, REQUEST
    )
    assert moved.current_version_id == second.version.id
    rolled_back = await catalog.set_current_version(
        actor, workspace_id, skill.id, first, REQUEST
    )
    assert rolled_back.current_version_id == first
    await catalog.withdraw_version(actor, workspace_id, skill.id, second.version.id, REQUEST)
    with pytest.raises(VersionNotBindable):
        await catalog.set_current_version(
            actor, workspace_id, skill.id, second.version.id, REQUEST
        )


async def test_a_platform_admin_outside_the_workspace_leaves_an_audit_trail(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """§9.3: acting on a workspace one is not a member of is allowed and never
    silent."""
    await create(catalog, platform_admin(), workspace_id)
    assert "skill.write_by_platform_admin" in store.audit_actions
