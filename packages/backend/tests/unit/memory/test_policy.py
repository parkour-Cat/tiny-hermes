"""What a workspace does with a memory somebody proposed.

§14.1's three choices, as one decision. Three things are pinned here that the
rest of the phase depends on.

`off` **stores nothing** — not a rejected row, not a note that something was
proposed. A workspace that turned memory off did not ask for a queue of things
it will not read.

`low_risk_auto` is a **widening of `all_pending`, never a bypass**: a candidate
the rules call risky still waits, and the test says so directly rather than
letting it follow from the branch order.

And a **shared candidate never takes the automatic path**, whatever the policy
is. §14.2 gives shared memory two ways in and neither of them is a rule.
"""

import pytest
from tiny_hermes.memory.domain.policy import (
    CandidateOutcome,
    MemoryPolicy,
    decide,
    status_for,
)
from tiny_hermes.memory.domain.scope import MemoryKind, MemoryStatus


def test_a_workspace_with_memory_off_stores_nothing() -> None:
    decision = decide(MemoryPolicy.OFF, MemoryKind.PRIVATE, low_risk=True)

    assert decision.outcome is CandidateOutcome.REFUSED
    assert not decision.stores_anything
    assert "turned off" in decision.reason


def test_off_refuses_a_low_risk_candidate_too() -> None:
    """The rules are not consulted at all: off is a decision about the
    feature, not about the content."""
    assert decide(MemoryPolicy.OFF, MemoryKind.PRIVATE, low_risk=True).outcome is (
        CandidateOutcome.REFUSED
    )


def test_the_default_policy_writes_a_candidate_and_waits() -> None:
    decision = decide(MemoryPolicy.ALL_PENDING, MemoryKind.PRIVATE, low_risk=True)

    assert decision.outcome is CandidateOutcome.PENDING
    assert decision.stores_anything


def test_all_pending_ignores_the_rules() -> None:
    """Under this policy the rule check has no vote. A deployment that chose
    "everything waits" chose it for the low-risk ones as well."""
    for low_risk in (True, False):
        assert decide(
            MemoryPolicy.ALL_PENDING, MemoryKind.PRIVATE, low_risk=low_risk
        ).outcome is CandidateOutcome.PENDING


def test_low_risk_auto_writes_a_low_risk_private_candidate() -> None:
    decision = decide(MemoryPolicy.LOW_RISK_AUTO, MemoryKind.PRIVATE, low_risk=True)

    assert decision.outcome is CandidateOutcome.WRITTEN


def test_low_risk_auto_still_makes_a_risky_candidate_wait() -> None:
    """A widening, not a bypass — asserted directly rather than left to follow
    from which branch comes first."""
    decision = decide(MemoryPolicy.LOW_RISK_AUTO, MemoryKind.PRIVATE, low_risk=False)

    assert decision.outcome is CandidateOutcome.PENDING


@pytest.mark.parametrize("policy", list(MemoryPolicy))
def test_a_shared_candidate_is_never_written_automatically(
    policy: MemoryPolicy,
) -> None:
    """§14.2 gives shared memory two ways in: an administrator's edit and an
    approved proposal. A policy is neither."""
    decision = decide(policy, MemoryKind.SHARED, low_risk=True)

    assert decision.outcome is not CandidateOutcome.WRITTEN


def test_a_written_candidate_becomes_active_and_a_pending_one_pending() -> None:
    assert status_for(CandidateOutcome.WRITTEN) is MemoryStatus.ACTIVE
    assert status_for(CandidateOutcome.PENDING) is MemoryStatus.PENDING
