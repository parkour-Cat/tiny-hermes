"""What a child Agent may do, and the direction the answer can move in.

§13's sixth clause: a child's permissions are the intersection of its parent's
and the delegation's, across six faces, and cannot be wider than the parent's.

Most of this file is one face at a time. The test that carries the rule,
though, is the parameterized one at the bottom: across every combination of
faces, `intersect` never returns a name that was not already in *every*
argument. That is the property, and the design decision underneath it is that
widening is not forbidden — it is unwriteable, because there is no union, no
`widen`, and no argument that adds anything.
"""

from collections.abc import Awaitable, Callable
from itertools import combinations
from typing import Any, cast

import pytest
from tiny_hermes.agents.domain.delegation import (
    DelegationScope,
    MemoryPermission,
    intersect,
)

FACES = ("tools", "files", "network", "secrets", "skills")


def scope(**overrides: object) -> DelegationScope:
    values: dict[str, object] = {
        "tools": ["shell.exec", "file.read"],
        "files": ["artifact-1"],
        "network": ["api.example.com"],
        "secrets": ["ORDERS_KEY"],
        "skills": ["version-1"],
        "memory": [MemoryPermission.READ_PRIVATE],
    }
    values.update(overrides)
    return DelegationScope.of(**values)  # type: ignore[arg-type]


# -- the empty answer --------------------------------------------------------


def test_no_layers_is_empty_rather_than_everything() -> None:
    """A vacuous "all" would turn a missing layer into a child holding more
    than anybody granted, and a missing layer is what a bug looks like."""
    assert intersect().empty


def test_an_empty_face_stays_empty() -> None:
    """A parent that holds no secrets delegates no secrets, however much the
    delegation asks for."""
    parent = scope(secrets=[])

    result = intersect(parent, scope())

    assert result.secrets == frozenset()


def test_a_delegation_that_asks_for_nothing_gets_nothing() -> None:
    result = intersect(scope(), DelegationScope())

    assert result.empty


# -- one face at a time ------------------------------------------------------


def test_only_the_tools_both_hold_survive() -> None:
    parent = scope(tools=["shell.exec", "file.read"])
    asked = scope(tools=["file.read", "file.write"])

    assert intersect(parent, asked).tools == frozenset({"file.read"})


def test_a_tool_the_parent_lacks_is_not_granted() -> None:
    """The rule, at the face it is most likely to be tried on."""
    parent = scope(tools=["file.read"])
    asked = scope(tools=["shell.exec"])

    assert intersect(parent, asked).tools == frozenset()


def test_files_are_artifact_ids_and_intersect_like_the_rest() -> None:
    parent = scope(files=["artifact-1", "artifact-2"])
    asked = scope(files=["artifact-2", "artifact-3"])

    assert intersect(parent, asked).files == frozenset({"artifact-2"})


def test_a_secret_reference_the_parent_does_not_hold_is_refused() -> None:
    parent = scope(secrets=["ORDERS_KEY"])
    asked = scope(secrets=["PAYROLL_KEY"])

    assert intersect(parent, asked).secrets == frozenset()


def test_skills_intersect_by_version_never_by_name() -> None:
    parent = scope(skills=["version-1", "version-2"])
    asked = scope(skills=["version-2"])

    assert intersect(parent, asked).skills == frozenset({"version-2"})


# -- memory, which is two permissions ----------------------------------------


def test_reading_and_proposing_are_separate_permissions() -> None:
    """§13's fifth clause. A child that may read what somebody said is not
    thereby a child that may write conclusions about them down."""
    parent = scope(
        memory=[MemoryPermission.READ_PRIVATE, MemoryPermission.PROPOSE_PRIVATE]
    )
    asked = scope(memory=[MemoryPermission.READ_PRIVATE])

    granted = intersect(parent, asked).memory

    assert granted == frozenset({MemoryPermission.READ_PRIVATE})
    assert MemoryPermission.PROPOSE_PRIVATE not in granted


def test_a_child_cannot_be_given_a_memory_permission_the_parent_lacks() -> None:
    parent = scope(memory=[MemoryPermission.READ_PRIVATE])
    asked = scope(memory=[MemoryPermission.PROPOSE_PRIVATE])

    assert intersect(parent, asked).memory == frozenset()


# -- the publish-time question -----------------------------------------------


def test_a_scope_covers_one_that_asks_for_less() -> None:
    assert scope().covers(scope(tools=["file.read"], secrets=[]))


def test_a_scope_does_not_cover_one_that_asks_for_more() -> None:
    assert not scope(tools=["file.read"]).covers(scope(tools=["shell.exec"]))


def test_what_is_missing_is_named_face_by_face() -> None:
    """A refusal that only said "too wide" would send an author guessing
    across six faces."""
    parent = scope(tools=["file.read"], secrets=[])
    asked = scope(tools=["shell.exec"], secrets=["PAYROLL_KEY"])

    missing = parent.missing_from(asked)

    assert missing["tools"] == ("shell.exec",)
    assert missing["secrets"] == ("PAYROLL_KEY",)
    assert "files" not in missing


def test_nothing_missing_is_an_empty_answer_rather_than_a_falsy_one() -> None:
    assert scope().missing_from(scope()) == {}


# -- the property, across every combination ----------------------------------


@pytest.mark.parametrize(
    "held",
    [set(combo) for size in range(len(FACES) + 1) for combo in combinations(FACES, size)],
)
def test_the_result_is_never_wider_than_the_parent(held: set[str]) -> None:
    """The rule itself, exhaustively: whatever the parent holds and whatever is
    asked for, every surviving name was already the parent's.

    Widening is not forbidden here — it is unwriteable. There is no union, no
    `widen`, and no argument that adds a name.
    """
    parent = DelegationScope.of(
        **{face: (["a", "b"] if face in held else []) for face in FACES},
        memory=[MemoryPermission.READ_PRIVATE] if "tools" in held else [],
    )
    asked = scope(
        tools=["a", "b", "c"],
        files=["a", "b", "c"],
        network=["a", "b", "c"],
        secrets=["a", "b", "c"],
        skills=["a", "b", "c"],
        memory=[MemoryPermission.READ_PRIVATE, MemoryPermission.PROPOSE_PRIVATE],
    )

    result = intersect(parent, asked)

    assert parent.covers(result)


def test_intersecting_three_layers_keeps_only_what_all_three_hold() -> None:
    """A chain, because a delegation may be measured against more than one
    thing — and the answer must not drift as layers are added."""
    first = scope(tools=["a", "b", "c"])
    second = scope(tools=["b", "c"])
    third = scope(tools=["c"])

    assert intersect(first, second, third).tools == frozenset({"c"})


def test_intersection_is_order_independent() -> None:
    first = scope(tools=["a", "b"])
    second = scope(tools=["b", "c"])

    assert intersect(first, second) == intersect(second, first)


# -- the spec field, and the hash it must not disturb ------------------------


def test_an_agent_that_delegates_to_nobody_hashes_as_it_always_did() -> None:
    """The eighth widening, and the same promise each of the last seven made.

    `AgentLimits` would have been the natural home for the parallel ceiling and
    is serialized into every spec — putting it there would have rewritten every
    content hash this platform has written.
    """
    from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec

    from .test_agent_models import valid_spec
    from .test_model_policy import DETERMINISTIC_HASH

    document, content_hash = normalize_agent_spec(AgentSpec.model_validate(valid_spec()))

    assert content_hash == DETERMINISTIC_HASH
    assert "delegation" not in document


def test_a_delegation_survives_normalization_and_changes_the_hash() -> None:
    from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec

    from .test_agent_models import valid_spec
    from .test_model_policy import DETERMINISTIC_HASH

    spec = AgentSpec.model_validate(
        {
            **valid_spec(),
            "delegation": {
                "children": [{"alias": "checker", "tools": ["file.read"]}],
                "max_parallel": 3,
            },
        }
    )

    document, content_hash = normalize_agent_spec(spec)

    delegation = cast(dict[str, Any], document["delegation"])
    assert delegation["max_parallel"] == 3
    assert content_hash != DETERMINISTIC_HASH


def test_the_same_child_bound_twice_is_refused() -> None:
    import pytest as _pytest
    from pydantic import ValidationError
    from tiny_hermes.agents.domain.models import AgentSpec

    from .test_agent_models import valid_spec

    with _pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "delegation": {
                    "children": [{"alias": "checker"}, {"alias": "checker"}],
                },
            }
        )


def test_a_delegation_with_no_children_is_refused() -> None:
    """An empty policy is not "delegates to nobody" — that is the absent key.
    A written policy that names nobody is a mistake."""
    import pytest as _pytest
    from pydantic import ValidationError
    from tiny_hermes.agents.domain.models import AgentSpec

    from .test_agent_models import valid_spec

    with _pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "delegation": {"children": []}})


# -- what publish refuses ----------------------------------------------------


async def _publishing(
    child_spec: dict[str, Any], *, parent: dict[str, Any] | None = None
) -> Callable[[], Awaitable[Any]]:
    """A workspace with a published `checker`, and a parent draft that
    delegates to it on the given terms."""
    from uuid import uuid4

    from tiny_hermes.agents.application.service import AgentCatalog
    from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
    from tiny_hermes.tenancy.domain.models import Actor, Role

    from .test_agent_models import valid_spec

    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    child = await catalog.create_agent(
        workspace_id, actor, "Checker", "checker", "req-0"
    )
    child_draft = await catalog.replace_draft(
        workspace_id, actor, child.id, 1, valid_spec(), "req-0b"
    )
    await catalog.publish(
        workspace_id, actor, child.id, child_draft.revision, "req-0c"
    )

    agent = await catalog.create_agent(workspace_id, actor, "Lead", "lead", "req-1")
    spec: dict[str, Any] = {**valid_spec(), **(parent or {}), "delegation": child_spec}
    draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, spec, "req-2"
    )

    async def publish() -> Any:
        return await catalog.publish(
            workspace_id, actor, agent.id, draft.revision, "req-3"
        )

    return publish


async def test_a_delegation_inside_the_parent_s_own_tools_publishes() -> None:
    from .test_agent_models import valid_spec

    publish = await _publishing(
        {"children": [{"alias": "checker", "tools": ["file.read"]}]},
        parent={"tools": ["file.read", "shell.exec"]},
    )
    del valid_spec

    result = await publish()

    assert result.version.version_number == 1


async def test_a_child_offered_a_tool_the_parent_lacks_is_refused() -> None:
    """§13's sixth clause, at publish rather than at runtime: the author is
    still holding it, and a Run would only have shown them an empty child."""
    from tiny_hermes.agents.application.service import DelegationTooWide

    publish = await _publishing(
        {"children": [{"alias": "checker", "tools": ["shell.exec"]}]},
        parent={"tools": ["file.read"]},
    )

    with pytest.raises(DelegationTooWide) as refused:
        await publish()

    assert refused.value.offending["checker"]["tools"] == ("shell.exec",)


async def test_a_child_offered_a_target_the_parent_cannot_reach_is_refused() -> None:
    from tiny_hermes.agents.application.service import DelegationTooWide

    publish = await _publishing(
        {"children": [{"alias": "checker", "network": ["api.example.com"]}]},
    )

    with pytest.raises(DelegationTooWide) as refused:
        await publish()

    assert "network" in refused.value.offending["checker"]


async def test_a_child_offered_a_memory_write_the_parent_lacks_is_refused() -> None:
    """A parent that cannot propose a memory cannot delegate proposing one."""
    from tiny_hermes.agents.application.service import DelegationTooWide

    publish = await _publishing(
        {
            "children": [
                {"alias": "checker", "memory": ["memory.propose_private"]}
            ]
        },
    )

    with pytest.raises(DelegationTooWide) as refused:
        await publish()

    assert "memory" in refused.value.offending["checker"]


async def test_delegating_to_an_alias_nobody_published_is_refused() -> None:
    """A delegation to a draft is a delegation to something no Run can start."""
    from tiny_hermes.agents.application.service import UnknownChildAgent

    publish = await _publishing({"children": [{"alias": "ghost"}]})

    with pytest.raises(UnknownChildAgent) as refused:
        await publish()

    assert refused.value.aliases == ("ghost",)
