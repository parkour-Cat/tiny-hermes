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

from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.goal import GoalOutcome, GoalVerdict
from tiny_hermes.runs.domain.models import PauseReason, RunSignal
from tiny_hermes.runs.domain.slice_policy import (
    WAIT_APPROVAL,
    RoundOutcome,
    decide_after_round,
)
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalVerdict

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


def test_a_wait_names_its_kind_and_carries_its_duration() -> None:
    """`RunStateMachine` refuses a `waiting_external` without a `wait_kind`, and
    it is right to: a Run waiting for nothing named is a Run nobody can tell
    how to wake. M2A ships one kind; approval and child runs reuse this seam.

    The duration travels as seconds rather than as a deadline because the
    deadline belongs to the moment the transition is *written*, not to the
    moment the round decided — those are two different clocks and one
    transaction apart.
    """
    decision = decide_after_round(
        RoundOutcome(
            verdict=WAIT,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert decision.wait_kind == "timer"
    assert decision.wait_seconds == 30


def test_nothing_but_a_wait_carries_a_wait() -> None:
    """A pause is not a wait with a shorter name.

    `RunStateMachine` refuses wait fields on any other target, so a decision
    that carried them by accident would turn a paused Run into a crash.
    """
    decision = decide_after_round(
        RoundOutcome(
            verdict=WAIT,
            cancel_requested=True,
            pause_requested=False,
            budget_allows=True,
            slice_expired=False,
        )
    )

    assert decision.signal is RunSignal.SAFE_CANCEL_STARTED
    assert decision.wait_kind is None
    assert decision.wait_seconds is None


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


# -- approvals ---------------------------------------------------------------


def _outcome(**overrides: object) -> RoundOutcome:
    fields: dict[str, object] = {
        "verdict": CONTINUE,
        "cancel_requested": False,
        "pause_requested": False,
        "budget_allows": True,
        "slice_expired": False,
    }
    fields.update(overrides)
    return RoundOutcome(**fields)  # type: ignore[arg-type]



def _check(
    verdict: ApprovalVerdict, expires_in: timedelta | None = timedelta(hours=1)
) -> ApprovalCheck:
    return ApprovalCheck(
        verdict,
        uuid4(),
        None if expires_in is None else datetime.now(UTC) + expires_in,
    )


def test_a_round_waiting_for_a_person_enters_waiting_approval() -> None:
    decision = decide_after_round(
        _outcome(approval=_check(ApprovalVerdict.REQUESTED))
    )

    assert decision.signal is RunSignal.APPROVAL_REQUESTED
    assert decision.wait_kind == WAIT_APPROVAL
    assert decision.wait_seconds is not None and decision.wait_seconds > 0


def test_a_run_already_being_asked_about_waits_too() -> None:
    decision = decide_after_round(_outcome(approval=_check(ApprovalVerdict.PENDING)))

    assert decision.signal is RunSignal.APPROVAL_REQUESTED


def test_nobody_who_may_decide_is_a_pause_and_not_an_escalation() -> None:
    """§16.3: a Run that reaches a confirmation with no subject pauses. It does
    not quietly become an administrator's decision."""
    decision = decide_after_round(
        _outcome(approval=_check(ApprovalVerdict.UNAVAILABLE))
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.APPROVAL_UNAVAILABLE


def test_an_approval_with_no_deadline_pauses_rather_than_waiting_forever() -> None:
    decision = decide_after_round(
        _outcome(approval=_check(ApprovalVerdict.REQUESTED, None))
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.APPROVAL_UNAVAILABLE


def test_an_approval_already_out_of_time_pauses_as_expired() -> None:
    decision = decide_after_round(
        _outcome(approval=_check(ApprovalVerdict.REQUESTED, timedelta(seconds=-1)))
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.APPROVAL_EXPIRED


def test_a_cancellation_outranks_a_question_nobody_answered() -> None:
    """A person who asked the Run to stop does not have to answer a question
    first."""
    decision = decide_after_round(
        _outcome(approval=_check(ApprovalVerdict.REQUESTED), cancel_requested=True)
    )

    assert decision.signal is RunSignal.SAFE_CANCEL_STARTED


def test_the_shared_budget_outranks_it_too() -> None:
    decision = decide_after_round(
        _outcome(approval=_check(ApprovalVerdict.REQUESTED), budget_allows=False)
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.LIMIT


# -- stop_conditions.max_rounds (§12.3 最大连续轮数) --------------------------


def _at_round_ceiling(verdict: GoalVerdict) -> RoundOutcome:
    return RoundOutcome(
        verdict=verdict,
        cancel_requested=False,
        pause_requested=False,
        budget_allows=True,
        slice_expired=False,
        rounds_exhausted=True,
    )


def test_a_run_out_of_rounds_that_would_continue_pauses_at_its_limit() -> None:
    """Declared since M2A and read by nothing until now: an Agent published with
    `max_rounds` ran until some other valve stopped it."""
    decision = decide_after_round(_at_round_ceiling(CONTINUE))

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.LIMIT
    assert decision.limit_reached is True
    assert decision.limit_valve == "max_rounds"


def test_a_run_that_finishes_on_its_last_round_completes() -> None:
    """The ceiling stops the next round, not the one that already met the goal."""
    decision = decide_after_round(_at_round_ceiling(DONE))

    assert decision.signal is RunSignal.COMPLETED


def test_a_run_out_of_rounds_does_not_start_a_wait_it_could_not_follow() -> None:
    """Waking would mean another round, which the ceiling forbids."""
    decision = decide_after_round(
        _at_round_ceiling(GoalVerdict(GoalOutcome.WAIT, wait_seconds=60))
    )

    assert decision.pause_reason is PauseReason.LIMIT
    assert decision.limit_valve == "max_rounds"


def test_the_budget_valve_still_names_itself() -> None:
    decision = decide_after_round(
        RoundOutcome(
            verdict=CONTINUE,
            cancel_requested=False,
            pause_requested=False,
            budget_allows=False,
            slice_expired=False,
        )
    )

    assert decision.limit_valve == "budget"


# -- a finished round and the safety valve (§12.3, v2.13) ----------------------
#
# `budget_allows` asks whether the budget allows *another* round. A round that
# judged the goal done or failed has no other round, so the answer must not
# rewrite its ending: seen 2026-09-29, a long task that finished on its last
# allowed model call ended `paused(limit)` and kept its Session's head.


def test_a_round_that_met_the_goal_completes_with_no_budget_left() -> None:
    decision = decide_after_round(_outcome(verdict=DONE, budget_allows=False))

    assert decision.signal is RunSignal.COMPLETED
    assert decision.limit_reached is False


def test_a_round_that_failed_fails_with_no_budget_left() -> None:
    decision = decide_after_round(_outcome(verdict=FAILED, budget_allows=False))

    assert decision.signal is RunSignal.FAILED
    assert decision.limit_reached is False


@pytest.mark.parametrize("verdict", [CONTINUE, WAIT], ids=["continue", "wait"])
def test_a_round_that_needs_another_still_stops_at_the_limit(verdict: GoalVerdict) -> None:
    decision = decide_after_round(_outcome(verdict=verdict, budget_allows=False))

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.LIMIT


def test_a_finished_round_holding_an_approval_still_stops_at_the_limit() -> None:
    # The change is for rounds that end the Run. One that is still waiting on
    # a person has not ended it.
    decision = decide_after_round(
        _outcome(verdict=DONE, budget_allows=False, approval=_check(ApprovalVerdict.REQUESTED))
    )

    assert decision.pause_reason is PauseReason.LIMIT


def test_a_cancel_still_outranks_a_finished_round_at_the_limit() -> None:
    decision = decide_after_round(
        _outcome(verdict=DONE, budget_allows=False, cancel_requested=True)
    )

    assert decision.signal is RunSignal.SAFE_CANCEL_STARTED
