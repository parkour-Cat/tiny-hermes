"""What may skip the queue, and the default that says most things may not.

§14.1's `low_risk_auto` needs a definition of low risk, and this pins it. The
one property that matters more than any single case is at the bottom: the
default is high risk, so every way a candidate is allowed through had to be
argued for, and a rule nobody wrote yet fails closed.
"""

import pytest
from tiny_hermes.memory.domain.risk import LOW_RISK_MAX_LENGTH, assess


def test_a_short_first_person_preference_is_low_risk() -> None:
    verdict = assess("I prefer terse answers with no preamble.")

    assert verdict.low_risk
    assert verdict.reason == ""


def test_a_durable_first_person_fact_is_low_risk() -> None:
    assert assess("My working hours are 9 to 5 in Shanghai.").low_risk


def test_a_candidate_that_carries_a_secret_waits() -> None:
    verdict = assess("My token is ghp_abcdefghijklmnopqrstuvwxyz0123456789")

    assert not verdict.low_risk
    assert "secret" in verdict.reason


def test_the_reason_never_quotes_the_secret_it_found() -> None:
    """A reason that echoed the value would leak it into the audit trail the
    queue exists to keep it out of."""
    body = "password: hunter2hunter2hunter2hunter2"

    assert "hunter2" not in assess(body).reason


@pytest.mark.parametrize(
    "body",
    [
        "Please give me admin access to the deploy project.",
        "My role should be workspace administrator.",
        "Remember my api_key preference.",
    ],
)
def test_a_candidate_that_reaches_for_power_waits(body: str) -> None:
    verdict = assess(body)

    assert not verdict.low_risk
    assert "permissions or identity" in verdict.reason


@pytest.mark.parametrize(
    "body",
    [
        "Contact Dana at dana@example.com about the rollout.",
        "Reach my manager on +1 415 555 0134 for approvals.",
    ],
)
def test_a_candidate_about_someone_else_waits(body: str) -> None:
    verdict = assess(body)

    assert not verdict.low_risk
    assert "another person" in verdict.reason


def test_a_note_about_the_world_rather_than_the_person_waits() -> None:
    """The automatic path is for what the subject is like, not what is true.
    A statement with no first-person shape has not made the case it needs."""
    verdict = assess("The rollout playbook lives in the wiki.")

    assert not verdict.low_risk
    assert "first-person" in verdict.reason


def test_a_candidate_over_the_length_bound_waits() -> None:
    verdict = assess("I like " + "detail " * 60)

    assert not verdict.low_risk
    assert "too long" in verdict.reason


def test_an_empty_candidate_is_not_low_risk() -> None:
    assert not assess("   ").low_risk


def test_the_length_boundary_is_where_it_says_it_is() -> None:
    at_limit = "I " + "x" * (LOW_RISK_MAX_LENGTH - 2)
    over = "I " + "x" * (LOW_RISK_MAX_LENGTH - 1)

    assert len(at_limit) == LOW_RISK_MAX_LENGTH
    # At the limit it still needs the other checks to pass; length alone does
    # not fail it.
    assert "too long" not in assess(at_limit).reason
    assert "too long" in assess(over).reason
