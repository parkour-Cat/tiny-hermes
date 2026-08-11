from itertools import product

import pytest
from tiny_hermes.runs.domain.models import PauseReason, RunSignal
from tiny_hermes.runs.domain.slice_policy import RoundOutcome, decide_after_round
from tiny_hermes.runs.ports.model import StopReason

CASES = [
    # Cancel wins over everything, including a model that already finished.
    (
        RoundOutcome(
            stop_reason=StopReason.COMPLETED,
            cancel_requested=True,
            pause_requested=True,
            budget_allows=True,
            slice_expired=True,
        ),
        RunSignal.SAFE_CANCEL_STARTED,
        None,
    ),
    # Pause wins over a model that wants to continue and over slice expiry.
    (
        RoundOutcome(
            stop_reason=StopReason.CONTINUE,
            cancel_requested=False,
            pause_requested=True,
            budget_allows=True,
            slice_expired=True,
        ),
        RunSignal.SAFE_PAUSE_REACHED,
        PauseReason.MANUAL,
    ),
    # The safety valve wins over the model.
    (
        RoundOutcome(
            stop_reason=StopReason.CONTINUE,
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
            stop_reason=StopReason.COMPLETED,
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
            stop_reason=StopReason.FAILED,
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
            stop_reason=StopReason.CONTINUE,
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
            stop_reason=StopReason.CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert decision.signal is None
    assert decision.keeps_lease is True
    assert decision.limit_reached is False


def test_only_the_limit_pause_records_the_safety_valve_event() -> None:
    limited = decide_after_round(
        RoundOutcome(
            stop_reason=StopReason.CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=False,
            slice_expired=False,
        )
    )
    manual = decide_after_round(
        RoundOutcome(
            stop_reason=StopReason.CONTINUE,
            cancel_requested=False,
            pause_requested=True,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert limited.limit_reached is True
    assert manual.limit_reached is False


def test_a_finished_model_still_yields_to_an_exhausted_budget() -> None:
    decision = decide_after_round(
        RoundOutcome(
            stop_reason=StopReason.COMPLETED,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=False,
            slice_expired=False,
        )
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.LIMIT


@pytest.mark.parametrize(
    ("stop_reason", "cancel", "pause", "budget", "expired"),
    [
        (stop_reason, cancel, pause, budget, expired)
        for stop_reason, cancel, pause, budget, expired in product(
            StopReason, (True, False), (True, False), (True, False), (True, False)
        )
    ],
)
def test_every_combination_returns_a_documented_outcome(
    stop_reason: StopReason, cancel: bool, pause: bool, budget: bool, expired: bool
) -> None:
    decision = decide_after_round(
        RoundOutcome(
            stop_reason=stop_reason,
            cancel_requested=cancel,
            pause_requested=pause,
            budget_allows=budget,
            slice_expired=expired,
        )
    )

    # A tool call keeps the slice too, and for the reason the whole sandbox
    # design rests on: the container is warm and the loop is mid-thought.
    # Ending the slice to run one command would freeze and thaw between every
    # step, which is the thing §11.4 exists to avoid.
    keeps_going = (
        stop_reason in (StopReason.CONTINUE, StopReason.TOOL_CALL)
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
        assert decision.pause_reason in {PauseReason.MANUAL, PauseReason.LIMIT}
    else:
        assert decision.pause_reason is None
