"""Who a memory belongs to, and the question this module will not answer.

§14.1 isolates private memory by workspace, agent and subject. Most of this
file is one test per way two things that look alike are not the same scope —
because every one of those is a way A's preference reaches B, and none of them
would be noticed by anybody watching.

The test that matters most is the last one in the first section. There is no
constructor, no default and no combination of arguments that produces "every
subject's memory". That is the technique rather than a rule: a function that
could ask for all of an Agent's private memories would be used by a report on
its first day.
"""

from uuid import UUID, uuid4

import pytest
from tiny_hermes.memory.domain.scope import (
    MemoryKind,
    MemoryScope,
    scopes_for_run,
)
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType

WORKSPACE = uuid4()
AGENT = uuid4()


def subject(caller_id: UUID | None = None, kind: CallerType = CallerType.USER):
    return CallerIdentity(caller_type=kind, caller_id=caller_id or uuid4())


def private(**overrides: object) -> MemoryScope:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "agent_id": AGENT,
        "subject": subject(),
    }
    values.update(overrides)
    return MemoryScope.private(**values)  # type: ignore[arg-type]


# -- what is not the same scope ---------------------------------------------


def test_the_same_subject_in_the_same_agent_is_one_scope() -> None:
    person = subject()

    assert private(subject=person) == private(subject=person)


def test_two_subjects_are_two_scopes() -> None:
    """The first exit criterion, at the level where it is decided."""
    assert private(subject=subject()) != private(subject=subject())


def test_one_person_under_two_agents_is_two_scopes() -> None:
    """§14.1 keys on the Agent as well as the subject. What somebody told the
    HR assistant is not what they told the deploy bot."""
    person = subject()

    assert private(subject=person) != private(subject=person, agent_id=uuid4())


def test_one_person_in_two_workspaces_is_two_scopes() -> None:
    person = subject()

    assert private(subject=person) != private(subject=person, workspace_id=uuid4())


def test_a_user_and_a_service_account_with_one_id_are_two_subjects() -> None:
    """`caller_type` is part of the identity, not a label on it. Two directories
    can hand out the same uuid and mean two different somebodies."""
    shared_id = uuid4()

    assert private(subject=subject(shared_id, CallerType.USER)) != private(
        subject=subject(shared_id, CallerType.SERVICE_ACCOUNT)
    )


def test_there_is_no_way_to_ask_for_every_subject() -> None:
    """The technique, not the rule. A wildcard subject cannot be constructed,
    and a private scope cannot be built without one."""
    with pytest.raises(TypeError):
        MemoryScope.private(workspace_id=WORKSPACE, agent_id=AGENT)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        MemoryScope(workspace_id=WORKSPACE, agent_id=AGENT, kind=MemoryKind.PRIVATE)


# -- shared memory, which belongs to the Agent ------------------------------


def test_a_shared_scope_has_no_subject() -> None:
    scope = MemoryScope.shared(workspace_id=WORKSPACE, agent_id=AGENT)

    assert scope.subject is None
    assert scope.kind is MemoryKind.SHARED


def test_a_shared_memory_cannot_be_attributed_to_somebody() -> None:
    """The other half of the same guard: a shared memory with an owner would
    read as one person's while affecting everybody."""
    with pytest.raises(ValueError):
        MemoryScope(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            kind=MemoryKind.SHARED,
            subject=subject(),
        )


def test_shared_is_not_reachable_by_widening_a_private_scope() -> None:
    """Its own constructor on purpose. "Belongs to nobody in particular" must
    be chosen, never arrived at."""
    person = subject()

    assert private(subject=person) != MemoryScope.shared(
        workspace_id=WORKSPACE, agent_id=AGENT
    )


# -- what a Run may read ----------------------------------------------------


def test_a_run_reads_its_own_private_scope_and_its_agent_s_shared_one() -> None:
    person = subject()

    scopes = scopes_for_run(workspace_id=WORKSPACE, agent_id=AGENT, subject=person)

    assert scopes == (
        MemoryScope.private(workspace_id=WORKSPACE, agent_id=AGENT, subject=person),
        MemoryScope.shared(workspace_id=WORKSPACE, agent_id=AGENT),
    )


def test_private_comes_first_because_that_is_what_survives_a_trim() -> None:
    """The order is also the order of what is kept when the segment is over
    budget, so a subject's own statement about themselves outranks what the
    workspace decided about everybody."""
    scopes = scopes_for_run(workspace_id=WORKSPACE, agent_id=AGENT, subject=subject())

    assert scopes[0].kind is MemoryKind.PRIVATE
    assert scopes[1].kind is MemoryKind.SHARED


def test_a_scope_covers_only_itself() -> None:
    """Not a hierarchy, and not a containment test somebody could widen."""
    person = subject()
    mine = private(subject=person)

    assert mine.covers(mine)
    assert not mine.covers(private(subject=subject()))
    assert not mine.covers(MemoryScope.shared(workspace_id=WORKSPACE, agent_id=AGENT))
