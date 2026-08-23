from datetime import datetime

from tiny_hermes.runs.domain.models import (
    TERMINAL_STATES,
    PauseReason,
    RunSignal,
    RunState,
    RunStateView,
    StateDecision,
)

TRANSITIONS: dict[tuple[RunState, RunSignal], RunState] = {
    (RunState.QUEUED, RunSignal.LEASE_ACQUIRED): RunState.RUNNING,
    (RunState.QUEUED, RunSignal.PAUSE_REQUESTED): RunState.PAUSED,
    (RunState.QUEUED, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.RUNNING, RunSignal.APPROVAL_REQUESTED): RunState.WAITING_APPROVAL,
    (RunState.RUNNING, RunSignal.EXTERNAL_WAIT_STARTED): RunState.WAITING_EXTERNAL,
    (RunState.RUNNING, RunSignal.SLICE_ENDED): RunState.QUEUED,
    (RunState.RUNNING, RunSignal.SAFE_PAUSE_REACHED): RunState.PAUSED,
    (RunState.RUNNING, RunSignal.SAFE_CANCEL_STARTED): RunState.CANCELLING,
    (RunState.RUNNING, RunSignal.COMPLETED): RunState.COMPLETED,
    (RunState.RUNNING, RunSignal.FAILED): RunState.FAILED,
    (RunState.RUNNING, RunSignal.INTERRUPTED): RunState.INTERRUPTED,
    (RunState.WAITING_APPROVAL, RunSignal.APPROVAL_APPROVED): RunState.QUEUED,
    (RunState.WAITING_APPROVAL, RunSignal.APPROVAL_PAUSED): RunState.PAUSED,
    (RunState.WAITING_APPROVAL, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.WAITING_EXTERNAL, RunSignal.EXTERNAL_READY): RunState.QUEUED,
    (RunState.WAITING_EXTERNAL, RunSignal.EXTERNAL_PAUSED): RunState.PAUSED,
    (RunState.WAITING_EXTERNAL, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.PAUSED, RunSignal.RESUME_REQUESTED): RunState.QUEUED,
    (RunState.PAUSED, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.CANCELLING, RunSignal.SAFE_CANCEL_FINISHED): RunState.CANCELLED,
    (RunState.CANCELLING, RunSignal.INTERRUPTED): RunState.INTERRUPTED,
    (RunState.INTERRUPTED, RunSignal.RECOVERY_APPROVED): RunState.QUEUED,
    (RunState.INTERRUPTED, RunSignal.RECOVERY_FAILED): RunState.FAILED,
    (RunState.INTERRUPTED, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    # Design §9's one narrow addition: only after the Scheduler confirmed the
    # over-limit sandbox and volume are gone, and only with the recorded
    # reason. The store-level guard checks the cleanup-intent columns too.
    (RunState.INTERRUPTED, RunSignal.LIMIT_CLEANUP_CONFIRMED): RunState.PAUSED,
}

PAUSE_REASONS: dict[RunSignal, frozenset[PauseReason]] = {
    RunSignal.PAUSE_REQUESTED: frozenset({PauseReason.MANUAL}),
    RunSignal.APPROVAL_PAUSED: frozenset(
        {
            PauseReason.APPROVAL_EXPIRED,
            PauseReason.APPROVAL_REJECTED,
            PauseReason.APPROVAL_UNAVAILABLE,
            PauseReason.MANUAL,
        }
    ),
    RunSignal.EXTERNAL_PAUSED: frozenset(
        {PauseReason.EXTERNAL_TIMEOUT, PauseReason.MANUAL}
    ),
    RunSignal.SAFE_PAUSE_REACHED: frozenset(
        {
            PauseReason.MANUAL,
            PauseReason.LIMIT,
            PauseReason.CONTEXT_OVERFLOW,
            PauseReason.TOOL_BUDGET_EXCEEDED,
            PauseReason.COMPAT_TIMEOUT,
            PauseReason.OPERATOR,
            PauseReason.SYSTEM,
        }
    ),
    # Exactly one reason may pass this door; anything else is a caller
    # inventing a state the design does not have.
    RunSignal.LIMIT_CLEANUP_CONFIRMED: frozenset({PauseReason.LIMIT}),
}

RESUMING_SIGNALS = frozenset(
    {
        RunSignal.LEASE_ACQUIRED,
        RunSignal.RESUME_REQUESTED,
        RunSignal.APPROVAL_APPROVED,
        RunSignal.EXTERNAL_READY,
        RunSignal.RECOVERY_APPROVED,
    }
)


class RunStateError(Exception):
    pass


class InvalidStateTransition(RunStateError):
    pass


class InvalidStateMetadata(RunStateError):
    pass


class RunLimitReached(RunStateError):
    pass


class RunStateMachine:
    """The only code allowed to choose a Run state change.

    Callers submit the fact that happened; the machine answers with the single
    mutation that the product matrix permits.
    """

    def decide(
        self,
        view: RunStateView,
        signal: RunSignal,
        *,
        pause_reason: PauseReason | None = None,
        wait_kind: str | None = None,
        wait_deadline_at: datetime | None = None,
    ) -> StateDecision:
        request = self._request_only(view, signal)
        if request is not None:
            return request

        target = TRANSITIONS.get((view.state, signal))
        if target is None:
            raise InvalidStateTransition

        if target is RunState.PAUSED:
            pause_reason = self._pause_reason(signal, pause_reason)
        elif pause_reason is not None:
            raise InvalidStateMetadata

        if target in {RunState.QUEUED, RunState.RUNNING} and not view.budget_allows_execution:
            raise RunLimitReached

        if target is RunState.WAITING_APPROVAL:
            return self._waiting_approval(signal, wait_kind, wait_deadline_at)
        if target is RunState.WAITING_EXTERNAL:
            return self._waiting_external(signal, wait_kind, wait_deadline_at)
        if wait_kind is not None or wait_deadline_at is not None:
            raise InvalidStateMetadata

        return StateDecision(
            state=target,
            signal=signal,
            pause_reason=pause_reason,
            clear_pause_request=signal in RESUMING_SIGNALS or target is RunState.PAUSED,
            clear_cancel_request=target is RunState.CANCELLED,
            starts_execution=target is RunState.RUNNING,
        )

    def available_actions(
        self,
        view: RunStateView,
        *,
        can_control: bool,
        can_retry: bool,
        can_hold_budget: bool = False,
    ) -> tuple[str, ...]:
        if view.state in TERMINAL_STATES:
            if view.state is RunState.FAILED and can_control and can_retry:
                return ("retry",)
            return ()
        if not can_control:
            return ()

        actions: list[str] = []
        if view.state in {RunState.QUEUED, RunState.RUNNING} and not view.pause_requested:
            actions.append("pause")
        if view.state is RunState.PAUSED and view.budget_allows_execution:
            actions.append("resume")
        if (
            view.state is RunState.PAUSED
            and not view.budget_allows_execution
            and can_hold_budget
        ):
            # §26's 安全阀管理. `resume` is withheld above because it would
            # fail, which leaves this Run with nothing but `cancel` — the
            # valve had no release, and raising the ceiling was reachable
            # only from an API client. Offered *only* here: on a healthy
            # budget it would read as a routine control rather than the
            # thing you reach for when a ceiling stopped the work.
            actions.append("widen_budget")
        if view.state is not RunState.CANCELLING and not view.cancel_requested:
            actions.append("cancel")
        return tuple(actions)

    def _request_only(self, view: RunStateView, signal: RunSignal) -> StateDecision | None:
        if view.state is not RunState.RUNNING:
            return None
        if signal is RunSignal.PAUSE_REQUESTED:
            return StateDecision(
                state=RunState.RUNNING, signal=signal, set_pause_requested=True
            )
        if signal is RunSignal.CANCEL_REQUESTED:
            return StateDecision(
                state=RunState.RUNNING, signal=signal, set_cancel_requested=True
            )
        return None

    def _pause_reason(
        self, signal: RunSignal, pause_reason: PauseReason | None
    ) -> PauseReason:
        allowed = PAUSE_REASONS[signal]
        if signal is RunSignal.PAUSE_REQUESTED and pause_reason is None:
            return PauseReason.MANUAL
        if pause_reason is None or pause_reason not in allowed:
            raise InvalidStateMetadata
        return pause_reason

    def _waiting_approval(
        self, signal: RunSignal, wait_kind: str | None, wait_deadline_at: datetime | None
    ) -> StateDecision:
        if wait_kind is None or not wait_kind.strip():
            raise InvalidStateMetadata
        return StateDecision(
            state=RunState.WAITING_APPROVAL,
            signal=signal,
            wait_kind=wait_kind,
            wait_deadline_at=wait_deadline_at,
        )

    def _waiting_external(
        self, signal: RunSignal, wait_kind: str | None, wait_deadline_at: datetime | None
    ) -> StateDecision:
        if wait_deadline_at is None:
            raise InvalidStateMetadata
        return StateDecision(
            state=RunState.WAITING_EXTERNAL,
            signal=signal,
            wait_kind=wait_kind or "external_condition",
            wait_deadline_at=wait_deadline_at,
        )
