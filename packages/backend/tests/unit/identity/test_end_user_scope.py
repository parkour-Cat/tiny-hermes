"""`MemoryScope` after `CallerType` grows a third member.

Design §3 promises the isolation logic does not change shape when the subject
becomes an `EndUser` — M2D already proved a wildcard subject is unconstructible
for `user` and `service_account` (`tests/unit/memory/test_scope.py`); this file
proves the same refusal holds for `end_user` now that it exists, without
touching `MemoryScope` itself.
"""

from uuid import uuid4

import pytest
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryScope
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType

WORKSPACE = uuid4()
AGENT = uuid4()


def test_an_end_user_is_a_valid_private_memory_subject() -> None:
    end_user = CallerIdentity(caller_type=CallerType.END_USER, caller_id=uuid4())

    scope = MemoryScope.private(workspace_id=WORKSPACE, agent_id=AGENT, subject=end_user)

    assert scope.subject == end_user
    assert scope.kind is MemoryKind.PRIVATE


def test_an_end_user_and_a_user_with_one_id_are_two_subjects() -> None:
    """`caller_type` is part of the identity. §4.5.1's whole point is that
    this platform's `users` and an enterprise's end users are never the same
    directory, and a shared uuid must not make them the same scope."""
    shared_id = uuid4()

    end_user_scope = MemoryScope.private(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        subject=CallerIdentity(caller_type=CallerType.END_USER, caller_id=shared_id),
    )
    user_scope = MemoryScope.private(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        subject=CallerIdentity(caller_type=CallerType.USER, caller_id=shared_id),
    )

    assert end_user_scope != user_scope


def test_there_is_still_no_way_to_ask_for_every_subject() -> None:
    """The technique M2D proved does not erode as `CallerType` grows. A
    wildcard subject was unconstructible with two members and stays
    unconstructible with three — `MemoryScope` was never told which."""
    with pytest.raises(TypeError):
        MemoryScope.private(workspace_id=WORKSPACE, agent_id=AGENT)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        MemoryScope(workspace_id=WORKSPACE, agent_id=AGENT, kind=MemoryKind.PRIVATE)
