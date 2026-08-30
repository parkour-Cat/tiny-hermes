from dataclasses import dataclass
from datetime import UTC, datetime

from tiny_hermes.runs.domain.goal import GoalOutcome, GoalVerdict
from tiny_hermes.runs.domain.models import PauseReason, RunSignal, WaitPolicy
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalVerdict
from tiny_hermes.runs.ports.children import DelegationWait

#: The only wait M2A produces (design §4.6): a duration, and a Scheduler that
#: re-queues the Run when it is up.
WAIT_TIMER = "timer"

#: What a Run in `waiting_approval` is waiting for. `RunStateMachine` refuses
#: that state without a kind, and naming it here rather than at the call site
#: keeps the string one thing rather than three spellings.
WAIT_APPROVAL = "approval"

#: §13's wait: this Run handed work to children and is hanging on their
#: outcomes. The third value rather than the third branch — M2A wrote down that
#: `approval` and `child_runs` would arrive this way, and the state, the
#: transition and the deadline are all the same ones `timer` uses.
WAIT_CHILD_RUNS = "child_runs"


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
    #: Set when a call this round made needs a person's decision and did not
    #: already have one. Carries which of the three not-approved answers came
    #: back, because two of them stop the Run for good and one only pauses it
    #: until somebody clicks.
    approval: ApprovalCheck | None = None
    #: Set when this round delegated work and the children now exist. Carries
    #: how long to wait and whether one answer is enough, because both are
    #: decided when the delegation is made rather than when it is settled.
    delegated: DelegationWait | None = None
    #: §12.1: whether a message arrived in this Run's Session after this Run
    #: started executing (v2.9.1 — not merely queued behind it; see
    #: `SqlRunStore.has_waiting_run`). This module is I/O-free and cannot look
    #: that up itself — the caller (the Worker) queries it and hands the
    #: answer in as a fact, the same way `cancel_requested` and
    #: `pause_requested` arrive as facts rather than lookups.
    user_waiting: bool = False


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
    #: Whether every child must finish or one is enough. Only ever set beside
    #: `WAIT_CHILD_RUNS`: a timer and an approval each wait on exactly one
    #: thing, so there is nothing for a policy to choose between.
    wait_policy: WaitPolicy | None = None

    @property
    def keeps_lease(self) -> bool:
        return self.signal is None


def _awaiting(check: "ApprovalCheck") -> SliceDecision:
    """What a not-approved answer does to the Run. §16.3, three outcomes.

    Waiting is the ordinary one. The other two are Runs that will not resume on
    their own: `unavailable` means there is nobody who may decide — which the
    section requires be a pause rather than a silent escalation — and a gate
    that answered nothing at all is treated the same way, because a Run left in
    `waiting_approval` with no row to answer would wait forever.
    """
    if check.verdict is ApprovalVerdict.UNAVAILABLE:
        return SliceDecision(
            RunSignal.SAFE_PAUSE_REACHED, PauseReason.APPROVAL_UNAVAILABLE
        )
    if check.expires_at is None:
        # A gate that asked nobody and named no deadline would leave a Run in
        # `waiting_approval` with nothing to answer and nothing to expire it.
        return SliceDecision(
            RunSignal.SAFE_PAUSE_REACHED, PauseReason.APPROVAL_UNAVAILABLE
        )
    remaining = int((check.expires_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        # Asked and already out of time. Pausing now says so, rather than
        # entering a wait the next sweep would end anyway.
        return SliceDecision(
            RunSignal.SAFE_PAUSE_REACHED, PauseReason.APPROVAL_EXPIRED
        )
    return SliceDecision(
        RunSignal.APPROVAL_REQUESTED,
        wait_kind=WAIT_APPROVAL,
        wait_seconds=remaining,
    )


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
    if outcome.approval is not None:
        # Above the verdict, below the user's own signals. A round that stopped
        # for an approval produced no tool result, so the judge is looking at a
        # round that did nothing — its answer is not the one that should decide
        # what happens to the Run. A cancel or a pause still outranks this: a
        # person who asked the Run to stop does not have to answer a question
        # first.
        return _awaiting(outcome.approval)
    if outcome.delegated is not None:
        # Above the verdict and below the user's own signals, in the same place
        # and for the same reason an approval sits there: a round that
        # delegated has not finished its work, and the judge is looking at a
        # round whose answers do not exist yet. A cancel, a pause or a spent
        # budget still outranks it — children that were just created are
        # children that will be cancelled with their parent.
        return SliceDecision(
            RunSignal.EXTERNAL_WAIT_STARTED,
            wait_kind=WAIT_CHILD_RUNS,
            wait_seconds=outcome.delegated.seconds,
            wait_policy=outcome.delegated.policy,
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
    if outcome.user_waiting:
        # §12.1: a round judged `continue`, with a message that arrived in
        # this Run's Session after this Run started, gives up the head so
        # that message gets handled. Below cancel, pause, budget, approval and
        # delegation — those are "must stop"; this is "should stop" — and
        # below done/failed/undecidable/wait, which have already decided
        # where this Run goes; preemption only claims the round that would
        # otherwise have kept going.
        #
        # `COMPLETED`, not a pause: a paused Run still holds the Session head
        # (§564 — only a terminal state releases it), so pausing would leave
        # the queued message exactly as stuck as it was. The goal was not
        # met; `goal_preempted` on the snapshot is what says so.
        return SliceDecision(RunSignal.COMPLETED)
    if outcome.compat_window_expired:
        return SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.COMPAT_TIMEOUT)
    if outcome.slice_expired:
        if outcome.hold_slice:
            return SliceDecision(None)
        return SliceDecision(RunSignal.SLICE_ENDED)
    return SliceDecision(None)
