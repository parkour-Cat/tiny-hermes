"""What ends a round, in the order the product requires.

The policy used to read the provider's `StopReason` directly, which made the
model the thing that decided a Run was over. It now reads a `GoalVerdict` — the
platform's answer to whether the goal was met — and the order around it is
unchanged: a user's cancellation and pause and the shared safety valve all
outrank a verdict of `done`, and slice expiry is the last word only when
nothing else applies.

This is the one place that turns a verdict into a signal. The judge does not
decide Run state and `RunStateMachine` is still the only authority over it.
"""

from itertools import product

import pytest
from tiny_hermes.runs.domain.goal import GoalOutcome, GoalVerdict
from tiny_hermes.runs.domain.models import PauseReason, RunSignal
from tiny_hermes.runs.domain.slice_policy import RoundOutcome, decide_after_round

DONE = GoalVerdict(GoalOutcome.DONE)
CONTINUE = GoalVerdict(GoalOutcome.CONTINUE)
FAILED = GoalVerdict(GoalOutcome.FAILED)
WAIT = GoalVerdict(GoalOutcome.WAIT, wait_seconds=30)
UNDECIDABLE = GoalVerdict(GoalOutcome.UNDECIDABLE)

CASES = [
    # Cancel wins over everything, including a goal the platform judged met.
    (
        RoundOutcome(
            verdict=DONE,
            cancel_requested=True,
            pause_requested=True,
            budget_allows=True,
            slice_expired=True,
        ),
        RunSignal.SAFE_CANCEL_STARTED,
        None,
    ),
    # Pause wins over a round that would continue, and over slice expiry.
    (
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=True,
            budget_allows=True,
            slice_expired=True,
        ),
        RunSignal.SAFE_PAUSE_REACHED,
        PauseReason.MANUAL,
    ),
    # The safety valve wins over the loop.
    (
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=False,
            slice_expired=False,
        ),
        RunSignal.SAFE_PAUSE_REACHED,
        PauseReason.LIMIT,
    ),
    (
        RoundOutcome(
            verdict=DONE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        ),
        RunSignal.COMPLETED,
        None,
    ),
    (
        RoundOutcome(
            verdict=FAILED,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        ),
        RunSignal.FAILED,
        None,
    ),
    (
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=True,
        ),
        RunSignal.SLICE_ENDED,
        None,
    ),
]

DOCUMENTED_SIGNALS = {
    RunSignal.SAFE_CANCEL_STARTED,
    RunSignal.SAFE_PAUSE_REACHED,
    RunSignal.COMPLETED,
    RunSignal.FAILED,
    RunSignal.SLICE_ENDED,
    RunSignal.EXTERNAL_WAIT_STARTED,
}


@pytest.mark.parametrize(("outcome", "signal", "reason"), CASES)
def test_precedence(
    outcome: RoundOutcome, signal: RunSignal, reason: PauseReason | None
) -> None:
    decision = decide_after_round(outcome)

    assert decision.signal is signal
    assert decision.pause_reason is reason
    assert decision.keeps_lease is False


def test_a_continuing_round_inside_budget_and_slice_keeps_the_lease() -> None:
    decision = decide_after_round(
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert decision.signal is None
    assert decision.keeps_lease is True
    assert decision.limit_reached is False


def test_a_completions_run_keeps_the_lease_across_an_ordinary_slice_boundary() -> None:
    decision = decide_after_round(
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=True,
            hold_slice=True,
        )
    )

    assert decision.signal is None
    assert decision.keeps_lease is True


def test_compat_window_expiry_pauses_instead_of_requeueing() -> None:
    decision = decide_after_round(
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=True,
            hold_slice=True,
            compat_window_expired=True,
        )
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.COMPAT_TIMEOUT


def test_a_met_goal_still_wins_over_the_compat_window() -> None:
    decision = decide_after_round(
        RoundOutcome(
            verdict=DONE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
            compat_window_expired=True,
        )
    )

    assert decision.signal is RunSignal.COMPLETED
    assert decision.pause_reason is None


def test_only_the_limit_pause_records_the_safety_valve_event() -> None:
    limited = decide_after_round(
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=False,
            slice_expired=False,
        )
    )
    manual = decide_after_round(
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=True,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert limited.limit_reached is True
    assert manual.limit_reached is False


@pytest.mark.parametrize("blocker", ["cancel", "pause", "budget"])
def test_a_met_goal_does_not_outrank_the_three_things_above_it(blocker: str) -> None:
    """The reason the judge does not decide Run state.

    `done` is an answer about the goal. Whether the Run may act on it is a
    different question, and it is answered here, where it was answered before
    the judge existed.
    """
    decision = decide_after_round(
        RoundOutcome(
            verdict=DONE,
            cancel_requested=blocker == "cancel",
            pause_requested=blocker == "pause",
            budget_allows=blocker != "budget",
            slice_expired=False,
        )
    )

    assert decision.signal is not RunSignal.COMPLETED


def test_a_verdict_the_platform_could_not_reach_pauses_for_an_operator() -> None:
    """§17.3: neither accept nor reject an outcome nobody observed."""
    decision = decide_after_round(
        RoundOutcome(
            verdict=UNDECIDABLE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.OPERATOR


def test_a_round_that_waits_gives_up_its_slice() -> None:
    """Waiting while holding a lease and a sandbox is the thing to avoid."""
    decision = decide_after_round(
        RoundOutcome(
            verdict=WAIT,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert decision.signal is RunSignal.EXTERNAL_WAIT_STARTED
    assert decision.keeps_lease is False


@pytest.mark.parametrize(
    ("outcome", "cancel", "pause", "budget", "expired"),
    [
        (outcome, cancel, pause, budget, expired)
        for outcome, cancel, pause, budget, expired in product(
            GoalOutcome, (True, False), (True, False), (True, False), (True, False)
        )
    ],
)
def test_every_combination_returns_a_documented_outcome(
    outcome: GoalOutcome, cancel: bool, pause: bool, budget: bool, expired: bool
) -> None:
    decision = decide_after_round(
        RoundOutcome(
            verdict=GoalVerdict(outcome, wait_seconds=30 if outcome is GoalOutcome.WAIT else None),
            cancel_requested=cancel,
            pause_requested=pause,
            budget_allows=budget,
            slice_expired=expired,
        )
    )

    # A round that continues keeps the slice, and for the reason the whole
    # sandbox design rests on: the container is warm and the loop is
    # mid-thought. Ending the slice to run one command would freeze and thaw
    # between every step, which is the thing §11.4 exists to avoid.
    keeps_going = (
        outcome is GoalOutcome.CONTINUE
        and not cancel
        and not pause
        and budget
        and not expired
    )
    if keeps_going:
        assert decision.signal is None
        assert decision.keeps_lease is True
        return

    assert decision.signal in DOCUMENTED_SIGNALS
    assert decision.keeps_lease is False
    if decision.signal is RunSignal.SAFE_PAUSE_REACHED:
        assert decision.pause_reason in {
            PauseReason.MANUAL,
            PauseReason.LIMIT,
            PauseReason.COMPAT_TIMEOUT,
            PauseReason.OPERATOR,
        }
    else:
        assert decision.pause_reason is None
