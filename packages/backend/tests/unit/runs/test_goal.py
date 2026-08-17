"""Who decides a Run is finished.

Through 0.1 the answer was the provider: `slice_policy` ended a Run on
`StopReason.COMPLETED`. Product design §531 says the model's judgement is a
proposal and the platform verifies it, so these tests describe a judge that
can disagree with a model that says it is done.

The judge answers one question — was the goal met — and nothing about what
state the Run goes to next. Cancellation, pause and the shared budget outrank
any verdict, and they keep doing so in `decide_after_round`, which already has
that order and already tests it.
"""

import pytest
from tiny_hermes.runs.domain.goal import (
    CompletionCheck,
    GoalEvidence,
    GoalOutcome,
    GoalProposal,
    judge,
)
from tiny_hermes.runs.ports.model import StopReason

UNDECLARED = GoalEvidence(declared=False)


def test_a_model_that_finishes_an_agent_without_conditions_still_finishes_it() -> None:
    """0.1's behaviour, reached through the new path.

    Every Agent published before this slice declares no completion condition,
    and none of them may change what they do.
    """
    verdict = judge(GoalProposal(StopReason.COMPLETED), UNDECLARED)

    assert verdict.outcome is GoalOutcome.DONE
    assert verdict.unmet == ()


def test_a_declared_goal_whose_checks_all_passed_is_done() -> None:
    evidence = GoalEvidence(
        declared=True,
        checks=(
            CompletionCheck(name="verification_command", met=True),
            CompletionCheck(name="/workspace/data/report.md", met=True),
        ),
    )

    assert judge(GoalProposal(StopReason.COMPLETED), evidence).outcome is GoalOutcome.DONE


def test_a_model_claiming_completion_against_a_failing_check_does_not_end_the_run() -> None:
    evidence = GoalEvidence(
        declared=True,
        checks=(
            CompletionCheck(name="verification_command", met=False),
            CompletionCheck(name="/workspace/data/report.md", met=True),
        ),
    )

    verdict = judge(GoalProposal(StopReason.COMPLETED), evidence)

    assert verdict.outcome is GoalOutcome.CONTINUE
    assert verdict.unmet == ("verification_command",)


def test_the_next_instruction_names_what_failed_and_not_what_passed() -> None:
    """The conversation already contains the task; repeating it is noise.

    What the round did not have is the reason the platform disagreed.
    """
    evidence = GoalEvidence(
        declared=True,
        checks=(
            CompletionCheck(name="tests pass", met=False),
            CompletionCheck(name="/workspace/data/report.md", met=True),
        ),
    )

    instruction = judge(GoalProposal(StopReason.COMPLETED), evidence).instruction

    assert instruction is not None
    assert "tests pass" in instruction
    assert "report.md" not in instruction


def test_checks_the_platform_could_not_run_are_not_a_verdict_either_way() -> None:
    """§17.3: do not continue past an outcome nobody observed.

    Treating an unrunnable check as failed would loop forever on a broken
    sandbox; treating it as passed would accept a claim on no evidence.
    """
    evidence = GoalEvidence(declared=True, checks=(), observable=False)

    verdict = judge(GoalProposal(StopReason.COMPLETED), evidence)

    assert verdict.outcome is GoalOutcome.UNDECIDABLE


def test_a_declared_goal_with_no_checks_run_yet_is_not_finished_by_the_model_alone() -> None:
    evidence = GoalEvidence(declared=True, checks=())

    assert judge(GoalProposal(StopReason.COMPLETED), evidence).outcome is GoalOutcome.CONTINUE


@pytest.mark.parametrize("declared", [True, False])
def test_a_round_that_asked_for_a_tool_is_never_a_completion(declared: bool) -> None:
    """The round produced a request, not an answer. Nothing to verify yet."""
    evidence = GoalEvidence(declared=declared)

    verdict = judge(GoalProposal(StopReason.TOOL_CALL), evidence)

    assert verdict.outcome is GoalOutcome.CONTINUE
    assert verdict.instruction is None


def test_a_failed_round_stays_failed_and_no_evidence_is_consulted() -> None:
    """A judge that could turn a failure into another round would hide it."""
    evidence = GoalEvidence(
        declared=True, checks=(CompletionCheck(name="tests pass", met=True),)
    )

    assert judge(GoalProposal(StopReason.FAILED), evidence).outcome is GoalOutcome.FAILED


def test_a_round_that_asked_to_wait_waits_and_carries_its_deadline() -> None:
    verdict = judge(GoalProposal(StopReason.CONTINUE, wait_seconds=30), UNDECLARED)

    assert verdict.outcome is GoalOutcome.WAIT
    assert verdict.wait_seconds == 30


def test_finishing_outranks_waiting() -> None:
    """A met goal has nothing left to wait for."""
    evidence = GoalEvidence(
        declared=True, checks=(CompletionCheck(name="tests pass", met=True),)
    )

    verdict = judge(GoalProposal(StopReason.COMPLETED, wait_seconds=30), evidence)

    assert verdict.outcome is GoalOutcome.DONE
    assert verdict.wait_seconds is None


def test_a_failed_round_does_not_wait() -> None:
    verdict = judge(GoalProposal(StopReason.FAILED, wait_seconds=30), UNDECLARED)

    assert verdict.outcome is GoalOutcome.FAILED


def test_a_wait_of_zero_or_less_is_not_a_wait() -> None:
    """A deadline already in the past would release the lease and wake at once."""
    verdict = judge(GoalProposal(StopReason.CONTINUE, wait_seconds=0), UNDECLARED)

    assert verdict.outcome is GoalOutcome.CONTINUE
