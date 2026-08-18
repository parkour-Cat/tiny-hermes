"""§15.3, from a suggestion to a version, and everywhere it stops short.

The roadmap's three exit checks for this stage are all here, each as a test
that fails loudly if somebody ever wires the shortcut:

* an unapproved proposal produces no version;
* after an approval the old version is still there to roll back to;
* a proposal the scan blocked cannot be published.

There is a fourth, from §15.3's last sentence — an Agent may not change a
version that is running. It holds by construction rather than by check, which
is exactly why it is written down as a test: nothing would stop a later change
that "helpfully" repointed a binding on approval.
"""

from uuid import UUID, uuid4

import pytest
from tiny_hermes.skills.application.service import (
    MAX_PROPOSALS_PER_RUN,
    ForbiddenSkillAction,
    InvalidSkillPackage,
    ProposalLimitReached,
    ProposalNotApprovable,
    SkillCatalog,
    SkillNameMismatch,
    UnknownProposal,
)
from tiny_hermes.skills.domain.models import (
    ProposalOrigin,
    ProposalStatus,
    Skill,
    SkillScope,
    SkillSource,
)
from tiny_hermes.skills.domain.package import SkillFile
from tiny_hermes.skills.infrastructure.memory_store import MemorySkillStore
from tiny_hermes.tenancy.domain.models import Actor, Role

REQUEST = "request-1"

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes

Read the house style and write one paragraph per kind of change.
"""

OTHER_MD = SKILL_MD.replace("name: release-notes", "name: postmortem")

#: The one finding that blocks. Prose the scanner dislikes would not do: only
#: credential material stops a package, by an explicit decision in `scan.py`.
A_LEAKED_KEY = "aws_secret_access_key = AKIAIOSFODNN7EXAMPLE"


def files(body: str = "Short sentences.", manifest: str = SKILL_MD) -> tuple[SkillFile, ...]:
    return (
        SkillFile(path="SKILL.md", text=manifest),
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


async def existing(
    catalog: SkillCatalog, actor: Actor, workspace_id: UUID, body: str = "Short sentences."
) -> Skill:
    skill, _ = await catalog.create_skill(
        actor, workspace_id, SkillScope.WORKSPACE, files(body), REQUEST
    )
    return skill


# -- opening one ------------------------------------------------------------


async def test_a_person_may_propose_a_change_to_a_skill_that_exists(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id)

    proposal = await catalog.propose(
        author, workspace_id, files("Shorter sentences."), REQUEST, skill_id=skill.id
    )

    assert proposal.status is ProposalStatus.PENDING
    assert proposal.origin is ProposalOrigin.HUMAN
    assert proposal.skill_id == skill.id
    # The base is where the diff starts, and it is the version this skill
    # currently offers rather than whatever was newest at approval time.
    assert proposal.base_version_id == skill.current_version_id


async def test_a_proposal_for_a_skill_that_does_not_exist_yet_names_itself(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)

    proposal = await catalog.propose(author, workspace_id, files(), REQUEST)

    assert proposal.skill_id is None
    assert proposal.base_version_id is None
    assert proposal.manifest.name == "release-notes"


async def test_a_proposal_may_not_rename_the_skill_it_changes(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """Same rule as upload: the name is what an Agent bound."""
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id)

    with pytest.raises(SkillNameMismatch):
        await catalog.propose(
            author,
            workspace_id,
            files(manifest=OTHER_MD),
            REQUEST,
            skill_id=skill.id,
        )


async def test_files_that_are_not_a_package_are_refused_before_anything_is_stored(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)

    with pytest.raises(InvalidSkillPackage):
        await catalog.propose(
            author, workspace_id, (SkillFile(path="notes.md", text="no manifest"),), REQUEST
        )

    assert await catalog.list_proposals(author, workspace_id, REQUEST) == []


async def test_a_viewer_may_read_the_queue_and_may_not_add_to_it(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    await catalog.propose(author, workspace_id, files(), REQUEST)
    viewer = member(store, workspace_id, Role.VIEWER)

    assert len(await catalog.list_proposals(viewer, workspace_id, REQUEST)) == 1
    with pytest.raises(ForbiddenSkillAction):
        await catalog.propose(viewer, workspace_id, files(), REQUEST)


async def test_blocking_content_is_stored_as_a_proposal_and_says_what_it_is(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """§15.3 step 3, first half.

    An upload carrying a key is refused outright, because a version is served
    into prompts. A proposal is served nowhere, and its author is holding forty
    files and has to be told which one they pasted a key into.
    """
    author = member(store, workspace_id, Role.DEVELOPER)

    proposal = await catalog.propose(author, workspace_id, files(A_LEAKED_KEY), REQUEST)

    assert proposal.status is ProposalStatus.PENDING
    # One line can match two patterns, and both are kept: a reviewer reading
    # "an AWS access key id" and "a secret assigned to a name that says so" is
    # better served than one shown whichever matched first.
    assert {finding.path for finding in proposal.findings} == {"style.md"}
    assert proposal.approvable is False


# -- the diff ---------------------------------------------------------------


async def test_reading_a_proposal_comes_with_the_difference_it_would_make(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id, "Short sentences.")
    proposal = await catalog.propose(
        author, workspace_id, files("Shorter sentences."), REQUEST, skill_id=skill.id
    )

    read, difference = await catalog.read_proposal(
        author, workspace_id, proposal.id, REQUEST
    )

    assert read.id == proposal.id
    assert [item.path for item in difference.files] == ["style.md"]
    assert difference.changed == 1


async def test_a_proposal_from_another_workspace_is_not_found(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose(author, workspace_id, files(), REQUEST)
    elsewhere = uuid4()
    stranger = member(store, elsewhere, Role.WORKSPACE_ADMIN)

    with pytest.raises(UnknownProposal):
        await catalog.read_proposal(stranger, elsewhere, proposal.id, REQUEST)


# -- deciding ---------------------------------------------------------------


async def test_an_unapproved_proposal_produces_no_version(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """The roadmap's first exit check for this stage."""
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id)
    before = await catalog.list_versions(author, workspace_id, skill.id, REQUEST)

    await catalog.propose(
        author, workspace_id, files("Shorter sentences."), REQUEST, skill_id=skill.id
    )

    after = await catalog.list_versions(author, workspace_id, skill.id, REQUEST)
    assert [item.id for item in after] == [item.id for item in before]


async def test_approving_publishes_an_immutable_version_that_says_where_it_came_from(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id)
    proposal = await catalog.propose(
        author, workspace_id, files("Shorter sentences."), REQUEST, skill_id=skill.id
    )
    reviewer = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    _, version = await catalog.approve_proposal(
        reviewer, workspace_id, proposal.id, REQUEST
    )

    assert version.version_number == 2
    assert version.source is SkillSource.PROPOSAL
    assert version.source_ref == str(proposal.id)
    decided, _ = await catalog.read_proposal(author, workspace_id, proposal.id, REQUEST)
    assert decided.status is ProposalStatus.APPROVED
    assert decided.decided_by == reviewer.id


async def test_the_version_the_proposal_started_from_is_still_there_afterwards(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """The roadmap's second exit check: after an approval, rollback is still
    possible. Nothing was replaced — a version was added beside the old one."""
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id, "Short sentences.")
    original = skill.current_version_id
    proposal = await catalog.propose(
        author, workspace_id, files("Shorter sentences."), REQUEST, skill_id=skill.id
    )
    await catalog.approve_proposal(author, workspace_id, proposal.id, REQUEST)

    versions = await catalog.list_versions(author, workspace_id, skill.id, REQUEST)
    assert original in {item.id for item in versions}
    assert original is not None
    rolled = await catalog.set_current_version(
        author, workspace_id, skill.id, original, REQUEST
    )
    assert rolled.current_version_id == original


async def test_approval_does_not_move_where_new_bindings_start(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """§15.3 step 6: switching is the Agent's explicit act, not a side effect.

    An approval that also repointed the skill would make "approve" and "put it
    in front of everyone who binds this next" one word.
    """
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id)
    proposal = await catalog.propose(
        author, workspace_id, files("Shorter sentences."), REQUEST, skill_id=skill.id
    )

    updated, version = await catalog.approve_proposal(
        author, workspace_id, proposal.id, REQUEST
    )

    assert updated.current_version_id == skill.current_version_id
    assert updated.current_version_id != version.id


async def test_an_approved_new_skill_starts_at_its_only_version(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """The exception, and the reason it is not one: there is nowhere else the
    default of a brand new skill could point."""
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose(author, workspace_id, files(), REQUEST)

    skill, version = await catalog.approve_proposal(
        author, workspace_id, proposal.id, REQUEST
    )

    assert skill.name == "release-notes"
    assert skill.current_version_id == version.id


async def test_a_proposal_the_scan_blocked_cannot_be_approved(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """The roadmap's third exit check, and §15.3 step 3's second half."""
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose(author, workspace_id, files(A_LEAKED_KEY), REQUEST)

    with pytest.raises(ProposalNotApprovable) as refused:
        await catalog.approve_proposal(author, workspace_id, proposal.id, REQUEST)

    assert {finding.path for finding in refused.value.findings} == {"style.md"}
    assert await catalog.list_skills(author, workspace_id, REQUEST) == []


async def test_rejecting_ends_it_and_creates_nothing(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose(author, workspace_id, files(), REQUEST)

    rejected = await catalog.reject_proposal(author, workspace_id, proposal.id, REQUEST)

    assert rejected.status is ProposalStatus.REJECTED
    assert rejected.decided_by == author.id
    assert await catalog.list_skills(author, workspace_id, REQUEST) == []


async def test_a_decided_proposal_cannot_be_decided_again(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """Two reviewers clicking approve produce one version, and the second is
    told why nothing happened."""
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose(author, workspace_id, files(), REQUEST)
    await catalog.approve_proposal(author, workspace_id, proposal.id, REQUEST)

    with pytest.raises(ProposalNotApprovable):
        await catalog.approve_proposal(author, workspace_id, proposal.id, REQUEST)
    with pytest.raises(ProposalNotApprovable):
        await catalog.reject_proposal(author, workspace_id, proposal.id, REQUEST)


async def test_a_viewer_may_not_decide(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose(author, workspace_id, files(), REQUEST)
    viewer = member(store, workspace_id, Role.VIEWER)

    with pytest.raises(ForbiddenSkillAction):
        await catalog.approve_proposal(viewer, workspace_id, proposal.id, REQUEST)
    with pytest.raises(ForbiddenSkillAction):
        await catalog.reject_proposal(viewer, workspace_id, proposal.id, REQUEST)


# -- from a Run -------------------------------------------------------------


async def test_an_agent_proposes_without_an_actor_and_gets_a_pending_row(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """No role is checked, because there is no interactive actor in a Run. What
    keeps it safe is the other end: this row is pending and no Run can approve
    one."""
    author = member(store, workspace_id, Role.DEVELOPER)
    skill = await existing(catalog, author, workspace_id)
    run_id = uuid4()

    proposal = await catalog.propose_from_run(
        workspace_id=workspace_id,
        run_id=run_id,
        created_by=author.id,
        files=files("Shorter sentences."),
        request_id=REQUEST,
        skill_id=skill.id,
        base_version_id=skill.current_version_id,
    )

    assert proposal.origin is ProposalOrigin.AGENT
    assert proposal.origin_run_id == run_id
    assert proposal.status is ProposalStatus.PENDING
    # Attributed to the person who published the Agent, which is the only user
    # on record who had anything to do with writing it.
    assert proposal.created_by == author.id


async def test_one_run_proposes_once(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    author = member(store, workspace_id, Role.DEVELOPER)
    run_id = uuid4()
    for _ in range(MAX_PROPOSALS_PER_RUN):
        await catalog.propose_from_run(
            workspace_id=workspace_id,
            run_id=run_id,
            created_by=author.id,
            files=files(),
            request_id=REQUEST,
        )

    with pytest.raises(ProposalLimitReached):
        await catalog.propose_from_run(
            workspace_id=workspace_id,
            run_id=run_id,
            created_by=author.id,
            files=files("Another idea."),
            request_id=REQUEST,
        )

    # A different Run is not affected: the ceiling is per Run, and a Session
    # that keeps going is not one Run.
    await catalog.propose_from_run(
        workspace_id=workspace_id,
        run_id=uuid4(),
        created_by=author.id,
        files=files("Another idea."),
        request_id=REQUEST,
    )


async def test_an_agents_proposal_still_cannot_approve_itself(
    catalog: SkillCatalog, store: MemorySkillStore, workspace_id: UUID
) -> None:
    """There is no method a Run could reach that approves anything.

    This test is the closest a Run can get: the proposal exists, and the only
    thing that turns one into a version needs an actor with a role.
    """
    author = member(store, workspace_id, Role.DEVELOPER)
    proposal = await catalog.propose_from_run(
        workspace_id=workspace_id,
        run_id=uuid4(),
        created_by=author.id,
        files=files(),
        request_id=REQUEST,
    )
    nobody = Actor(uuid4(), is_platform_admin=False)

    with pytest.raises(ForbiddenSkillAction):
        await catalog.approve_proposal(nobody, workspace_id, proposal.id, REQUEST)
    assert await catalog.list_skills(author, workspace_id, REQUEST) == []
