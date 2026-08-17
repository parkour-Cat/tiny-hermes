from dataclasses import dataclass

from tiny_hermes.runs.domain.goal import GoalOutcome, GoalVerdict
from tiny_hermes.runs.domain.models import PauseReason, RunSignal

#: The only wait M2A produces (design §4.6): a duration, and a Scheduler that
#: re-queues the Run when it is up.
WAIT_TIMER = "timer"


@dataclass(frozen=True)
class RoundOutcome:
    """Everything the policy is allowed to look at after one model round."""

    #: The platform's answer to whether the goal was met. Through 0.1 this was
    #: the provider's own `StopReason`, which made a model the thing that ended
    #: a Run. What the model reports is now one input to `goal.judge`.
    verdict: GoalVerdict
    cancel_requested: bool
    pause_requested: bool
    budget_allows: bool
    slice_expired: bool
    hold_slice: bool = False
    compat_window_expired: bool = False


@dataclass(frozen=True)
class SliceDecision:
    """What the Worker does next.

    ``signal`` is still only a request: ``RunStateMachine`` decides the state.
    """

    signal: RunSignal | None
    pause_reason: PauseReason | None = None
    limit_reached: bool = False
    #: What this Run is waiting for, set only alongside
    #: ``EXTERNAL_WAIT_STARTED``. ``RunStateMachine`` refuses a
    #: ``waiting_external`` without one, and refuses one on anything else.
    wait_kind: str | None = None
    #: How long, in seconds. Not a deadline: the deadline belongs to the moment
    #: the transition is written, which is a different clock and a transaction
    #: later than the moment this round decided.
    wait_seconds: int | None = None

    @property
    def keeps_lease(self) -> bool:
        return self.signal is None


def decide_after_round(outcome: RoundOutcome) -> SliceDecision:
    """Choose what ends this round.

    The order is the product rule, not an implementation convenience: a user's
    cancellation and pause and the shared safety valve all outrank a goal the
    platform judged met, and slice expiry is the last word only when nothing
    else applies.

    This is the single place a verdict becomes a signal. The judge answers what
    happened to the goal; what happens to the Run is decided here and settled
    by ``RunStateMachine``.
    """
    if outcome.cancel_requested:
        return SliceDecision(RunSignal.SAFE_CANCEL_STARTED)
    if outcome.pause_requested:
        return SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.MANUAL)
    if not outcome.budget_allows:
        return SliceDecision(
            RunSignal.SAFE_PAUSE_REACHED, PauseReason.LIMIT, limit_reached=True
        )
    if outcome.verdict.outcome is GoalOutcome.DONE:
        return SliceDecision(RunSignal.COMPLETED)
    if outcome.verdict.outcome is GoalOutcome.FAILED:
        return SliceDecision(RunSignal.FAILED)
    if outcome.verdict.outcome is GoalOutcome.UNDECIDABLE:
        # The checks could not be run, so the claim is neither accepted nor
        # rejected and a person is asked. Continuing would be guessing.
        return SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.OPERATOR)
    if outcome.verdict.outcome is GoalOutcome.WAIT:
        # Whatever this Run is waiting for, it is not the lease or the warm
        # sandbox it is holding.
        #
        # M2A ships one kind. `approval` (M2C) and `child_runs` (M2E) are the
        # same state reached the same way, so they arrive as another value
        # here rather than as another branch below.
        return SliceDecision(
            RunSignal.EXTERNAL_WAIT_STARTED,
            wait_kind=WAIT_TIMER,
            wait_seconds=outcome.verdict.wait_seconds,
        )
    if outcome.compat_window_expired:
        return SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.COMPAT_TIMEOUT)
    if outcome.slice_expired:
        if outcome.hold_slice:
            return SliceDecision(None)
        return SliceDecision(RunSignal.SLICE_ENDED)
    return SliceDecision(None)
