from dataclasses import dataclass

from tiny_hermes.runs.domain.models import PauseReason, RunSignal
from tiny_hermes.runs.ports.model import StopReason


@dataclass(frozen=True)
class RoundOutcome:
    """Everything the policy is allowed to look at after one model round."""

    stop_reason: StopReason
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

    @property
    def keeps_lease(self) -> bool:
        return self.signal is None


def decide_after_round(outcome: RoundOutcome) -> SliceDecision:
    """Choose what ends this round.

    The order is the product rule, not an implementation convenience: a user's
    cancellation and pause and the shared safety valve all outrank a model that
    would rather keep going, and slice expiry is the last word only when
    nothing else applies.
    """
    if outcome.cancel_requested:
        return SliceDecision(RunSignal.SAFE_CANCEL_STARTED)
    if outcome.pause_requested:
        return SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.MANUAL)
    if not outcome.budget_allows:
        return SliceDecision(
            RunSignal.SAFE_PAUSE_REACHED, PauseReason.LIMIT, limit_reached=True
        )
    if outcome.stop_reason is StopReason.COMPLETED:
        return SliceDecision(RunSignal.COMPLETED)
    if outcome.stop_reason is StopReason.FAILED:
        return SliceDecision(RunSignal.FAILED)
    if outcome.compat_window_expired:
        return SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.COMPAT_TIMEOUT)
    if outcome.slice_expired:
        if outcome.hold_slice:
            return SliceDecision(None)
        return SliceDecision(RunSignal.SLICE_ENDED)
    return SliceDecision(None)
