"""The rules the catalog keeps, stated against the store that has no database.

Every rule here is also a constraint in `20260817_0014_skills.py`. That is the
point of writing them twice: the migration is what production enforces, and
this file is what fails in half a second when somebody changes the shape.

Product design §15.1 for the two-level catalog, §15.3 for what may be approved.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tiny_hermes.skills.domain.models import (
    ProposalOrigin,
    ProposalStatus,
    Skill,
    SkillScope,
    SkillSource,
    SkillVersionStatus,
)
from tiny_hermes.skills.domain.package import SkillFile, SkillPackage, parse_package
from tiny_hermes.skills.domain.scan import Finding, Severity
from tiny_hermes.skills.infrastructure.memory_store import MemorySkillStore
from tiny_hermes.skills.ports.store import DuplicateSkillName, VersionResult

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes

Group the changelog by kind and write one paragraph per group.
"""


def package(body: str = "Short sentences.") -> SkillPackage:
    return parse_package(
        (
            SkillFile(path="SKILL.md", text=SKILL_MD),
            SkillFile(path="style.md", text=body),
        )
    )


@pytest.fixture
def store() -> MemorySkillStore:
    return MemorySkillStore()


async def workspace_skill(
    store: MemorySkillStore, workspace_id: UUID, name: str = "release-notes"
) -> Skill:
    return await store.create_skill(
        scope=SkillScope.WORKSPACE,
        workspace_id=workspace_id,
        name=name,
        created_by=uuid4(),
    )


async def add(
    store: MemorySkillStore,
    skill_id: UUID,
    body: str = "Short sentences.",
    findings: tuple[Finding, ...] = (),
) -> VersionResult:
    return await store.add_version(
        skill_id=skill_id,
        package=package(body),
        findings=findings,
        source=SkillSource.UPLOAD,
        source_url=None,
        source_ref=None,
        created_by=uuid4(),
    )


async def test_the_same_content_twice_does_not_make_a_second_version(
    store: MemorySkillStore,
) -> None:
    """Roadmap §5's exit check. `uq_skill_versions_content` says it in Postgres."""
    skill = await workspace_skill(store, uuid4())
    first = await add(store, skill.id)
    again = await add(store, skill.id)
    assert first.created is True
    assert again.created is False
    assert again.version.id == first.version.id
    assert len(await store.list_versions(skill.id)) == 1


async def test_changed_content_gets_the_next_number_and_keeps_the_old_one(
    store: MemorySkillStore,
) -> None:
    """Rollback needs the previous version to still be there."""
    skill = await workspace_skill(store, uuid4())
    first = await add(store, skill.id, "Short sentences.")
    second = await add(store, skill.id, "Shorter sentences.")
    assert [version.version_number for version in await store.list_versions(skill.id)] == [1, 2]
    assert (await store.read_files(first.version.id))[1].text == "Short sentences."
    assert (await store.read_files(second.version.id))[1].text == "Shorter sentences."


async def test_moving_the_default_version_leaves_every_version_alone(
    store: MemorySkillStore,
) -> None:
    """`current_version_id` is where new bindings start, not what Runs read."""
    skill = await workspace_skill(store, uuid4())
    first = await add(store, skill.id, "Short sentences.")
    second = await add(store, skill.id, "Shorter sentences.")
    await store.set_current_version(skill.id, second.version.id)
    rolled_back = await store.set_current_version(skill.id, first.version.id)
    assert rolled_back is not None
    assert rolled_back.current_version_id == first.version.id
    assert len(await store.list_versions(skill.id)) == 2
    assert (await store.get_version(second.version.id)) == second.version


async def test_one_workspace_never_sees_another_workspace_s_skill(
    store: MemorySkillStore,
) -> None:
    mine, theirs = uuid4(), uuid4()
    await workspace_skill(store, mine, name="mine")
    await workspace_skill(store, theirs, name="theirs")
    assert [skill.name for skill in await store.list_visible(mine)] == ["mine"]
    assert [skill.name for skill in await store.list_visible(theirs)] == ["theirs"]


async def test_a_platform_skill_is_visible_from_every_workspace(
    store: MemorySkillStore,
) -> None:
    """§9.3's workspace scoping has exactly one exception, and this is it."""
    await store.create_skill(
        scope=SkillScope.PLATFORM,
        workspace_id=None,
        name="house-style",
        created_by=uuid4(),
    )
    one, other = uuid4(), uuid4()
    assert [skill.name for skill in await store.list_visible(one)] == ["house-style"]
    assert [skill.name for skill in await store.list_visible(other)] == ["house-style"]


async def test_a_name_is_taken_once_per_scope_and_workspace(
    store: MemorySkillStore,
) -> None:
    """A workspace may keep its own `release-notes` beside the platform's."""
    workspace_id = uuid4()
    await workspace_skill(store, workspace_id)
    with pytest.raises(DuplicateSkillName):
        await workspace_skill(store, workspace_id)
    await store.create_skill(
        scope=SkillScope.PLATFORM,
        workspace_id=None,
        name="release-notes",
        created_by=uuid4(),
    )
    await workspace_skill(store, uuid4())


async def test_a_withdrawn_version_stays_readable_and_stops_being_bindable(
    store: MemorySkillStore,
) -> None:
    """A Run already bound to it must keep working; nothing new may pick it."""
    skill = await workspace_skill(store, uuid4())
    stored = await add(store, skill.id)
    assert stored.version.bindable
    withdrawn = await store.set_version_status(
        stored.version.id, SkillVersionStatus.WITHDRAWN
    )
    assert withdrawn is not None and not withdrawn.bindable
    assert len(await store.read_files(stored.version.id)) == 2


async def test_a_version_with_a_blocking_finding_is_not_bindable(
    store: MemorySkillStore,
) -> None:
    skill = await workspace_skill(store, uuid4())
    stored = await add(
        store,
        skill.id,
        findings=(
            Finding(
                code="credential_material",
                severity=Severity.BLOCKING,
                path="style.md",
                detail="contains what looks like a private key block",
            ),
        ),
    )
    assert not stored.version.bindable


async def test_an_advisory_finding_does_not_stop_a_binding(
    store: MemorySkillStore,
) -> None:
    """Blocking on everything the scan notices teaches people to dodge it."""
    skill = await workspace_skill(store, uuid4())
    stored = await add(
        store,
        skill.id,
        findings=(
            Finding(
                code="unreferenced_file",
                severity=Severity.ADVISORY,
                path="style.md",
                detail="nothing in this package links to it",
            ),
        ),
    )
    assert stored.version.bindable


async def test_a_proposal_with_a_blocking_finding_may_be_read_but_not_approved(
    store: MemorySkillStore,
) -> None:
    """§15.3 step 3. It exists so its author can see what they got wrong."""
    workspace_id = uuid4()
    skill = await workspace_skill(store, workspace_id)
    proposal = await store.create_proposal(
        workspace_id=workspace_id,
        skill_id=skill.id,
        base_version_id=None,
        package=package(),
        findings=(
            Finding(
                code="credential_material",
                severity=Severity.BLOCKING,
                path="style.md",
                detail="contains what looks like a GitHub token",
            ),
        ),
        origin=ProposalOrigin.AGENT,
        origin_run_id=uuid4(),
        created_by=uuid4(),
    )
    assert not proposal.approvable
    assert await store.get_proposal(proposal.id) == proposal


async def test_deciding_a_proposal_writes_no_version(store: MemorySkillStore) -> None:
    """The only path from a proposal to a version is §7's approval, which is
    a separate call. Nothing in the store may shortcut it."""
    workspace_id = uuid4()
    skill = await workspace_skill(store, workspace_id)
    proposal = await store.create_proposal(
        workspace_id=workspace_id,
        skill_id=skill.id,
        base_version_id=None,
        package=package(),
        findings=(),
        origin=ProposalOrigin.HUMAN,
        origin_run_id=None,
        created_by=uuid4(),
    )
    assert proposal.approvable
    decided = await store.decide_proposal(
        proposal.id, ProposalStatus.APPROVED, uuid4(), datetime.now(UTC)
    )
    assert decided is not None and decided.status is ProposalStatus.APPROVED
    assert await store.list_versions(skill.id) == []


async def test_a_proposal_is_decided_once(store: MemorySkillStore) -> None:
    workspace_id = uuid4()
    skill = await workspace_skill(store, workspace_id)
    proposal = await store.create_proposal(
        workspace_id=workspace_id,
        skill_id=skill.id,
        base_version_id=None,
        package=package(),
        findings=(),
        origin=ProposalOrigin.HUMAN,
        origin_run_id=None,
        created_by=uuid4(),
    )
    await store.decide_proposal(
        proposal.id, ProposalStatus.REJECTED, uuid4(), datetime.now(UTC)
    )
    again = await store.decide_proposal(
        proposal.id, ProposalStatus.APPROVED, uuid4(), datetime.now(UTC)
    )
    assert again is None


async def test_proposals_are_listed_inside_one_workspace_only(
    store: MemorySkillStore,
) -> None:
    mine, theirs = uuid4(), uuid4()
    for workspace_id in (mine, theirs):
        skill = await workspace_skill(store, workspace_id)
        await store.create_proposal(
            workspace_id=workspace_id,
            skill_id=skill.id,
            base_version_id=None,
            package=package(),
            findings=(),
            origin=ProposalOrigin.HUMAN,
            origin_run_id=None,
            created_by=uuid4(),
        )
    listed = await store.list_proposals(mine)
    assert [proposal.workspace_id for proposal in listed] == [mine]
    assert len(await store.list_proposals(mine, ProposalStatus.APPROVED)) == 0
