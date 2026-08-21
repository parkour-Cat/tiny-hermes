"""§4.6's 审计记录 row is five subjects and five ranges, not a switch. This
file pins the one property the plan calls out as the way this goes wrong:
`AuditScope` cannot express "every workspace's every row" — not forbidden,
unwriteable, the same technique `MemoryScope` uses (`memory/domain/scope.py`).

A platform administrator's widest constructible scope is still one
workspace's full trail — §3's cross-workspace bookkeeping is a decision the
*application* layer makes about whether to log a read, not something the
scope itself can widen past a single workspace to reach.
"""

from uuid import UUID, uuid4

import pytest
from tiny_hermes.audit.domain.scope import AuditScope, AuditVisibility

WORKSPACE = uuid4()
ACTOR = uuid4()


def test_full_scope_names_one_workspace_and_narrows_nothing_further() -> None:
    scope = AuditScope.full(workspace_id=WORKSPACE)

    assert scope.workspace_id == WORKSPACE
    assert scope.visibility is AuditVisibility.FULL
    assert scope.actor_id is None


def test_own_resources_scope_requires_whose_resources() -> None:
    scope = AuditScope.own_resources(workspace_id=WORKSPACE, actor_id=ACTOR)

    assert scope.visibility is AuditVisibility.OWN_RESOURCES
    assert scope.actor_id == ACTOR


def test_own_resources_cannot_be_constructed_without_an_actor() -> None:
    with pytest.raises(TypeError):
        AuditScope.own_resources(workspace_id=WORKSPACE)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        AuditScope(workspace_id=WORKSPACE, visibility=AuditVisibility.OWN_RESOURCES)


def test_redacted_scope_narrows_context_not_rows() -> None:
    scope = AuditScope.redacted(workspace_id=WORKSPACE)

    assert scope.visibility is AuditVisibility.REDACTED
    assert scope.actor_id is None


def test_full_and_redacted_scopes_refuse_an_actor_id() -> None:
    """The `actor_id` field means one thing — "narrow rows to this actor's
    own resources" — and only `OWN_RESOURCES` may carry it. A `FULL` or
    `REDACTED` scope built with one would silently narrow rows nobody asked
    to narrow, or read as "this is somebody's own scope" when it is not."""
    with pytest.raises(ValueError):
        AuditScope(workspace_id=WORKSPACE, visibility=AuditVisibility.FULL, actor_id=ACTOR)
    with pytest.raises(ValueError):
        AuditScope(
            workspace_id=WORKSPACE, visibility=AuditVisibility.REDACTED, actor_id=ACTOR
        )


def test_there_is_no_way_to_ask_for_every_workspace() -> None:
    """The technique this file exists to pin: no constructor, no default and
    no combination of arguments produces a scope spanning more than one
    workspace. `workspace_id` is a required positional-by-keyword field on
    every constructor and on the dataclass itself — there is no wildcard
    value, no `None`, and no `all_workspaces()` classmethod anywhere in this
    module."""
    with pytest.raises(TypeError):
        AuditScope.full()  # type: ignore[call-arg]
    # A plain dataclass does not enforce its own annotations at runtime, so
    # `workspace_id=None` is checked explicitly rather than left to the type
    # hint alone — the one caller a type checker cannot stop is a `# type:
    # ignore` away from constructing exactly the wildcard this module exists
    # to prevent.
    with pytest.raises(ValueError):
        AuditScope.full(workspace_id=None)  # type: ignore[arg-type]
    assert not hasattr(AuditScope, "all_workspaces")
    assert not hasattr(AuditScope, "everywhere")


def test_two_workspaces_are_two_scopes_even_at_the_same_visibility() -> None:
    assert AuditScope.full(workspace_id=WORKSPACE) != AuditScope.full(workspace_id=uuid4())


def test_workspace_id_type_is_not_optional() -> None:
    """A field typed `UUID | None` would let a future caller pass `None` and
    have it silently mean "every workspace" the day nobody is looking. The
    annotation itself is part of what makes "all of them" unwriteable."""
    import dataclasses

    field = {f.name: f for f in dataclasses.fields(AuditScope)}["workspace_id"]
    assert field.type is UUID


def test_workspace_id_is_actually_required_at_runtime() -> None:
    with pytest.raises(TypeError):
        AuditScope(visibility=AuditVisibility.FULL)  # type: ignore[call-arg]
