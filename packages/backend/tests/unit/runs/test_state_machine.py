from datetime import UTC, datetime, timedelta

import pytest
from tiny_hermes.runs.domain.models import (
    PauseReason,
    RunSignal,
    RunState,
    RunStateView,
    StateDecision,
)
from tiny_hermes.runs.domain.state_machine import InvalidStateTransition, RunStateMachine

ALLOWED = {
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
}

REQUEST_ONLY = {
    (RunState.RUNNING, RunSignal.PAUSE_REQUESTED),
    (RunState.RUNNING, RunSignal.CANCEL_REQUESTED),
}


def _decide(
    machine: RunStateMachine, view: RunStateView, signal: RunSignal
) -> StateDecision:
    if signal is RunSignal.SAFE_PAUSE_REACHED:
        return machine.decide(view, signal, pause_reason=PauseReason.LIMIT)
    if signal is RunSignal.APPROVAL_PAUSED:
        return machine.decide(view, signal, pause_reason=PauseReason.APPROVAL_EXPIRED)
    if signal is RunSignal.EXTERNAL_PAUSED:
        return machine.decide(view, signal, pause_reason=PauseReason.EXTERNAL_TIMEOUT)
    if signal is RunSignal.APPROVAL_REQUESTED:
        return machine.decide(view, signal, wait_kind="governance_approval")
    if signal is RunSignal.EXTERNAL_WAIT_STARTED:
        return machine.decide(
            view, signal, wait_deadline_at=datetime.now(UTC) + timedelta(minutes=5)
        )
    return machine.decide(view, signal)


@pytest.mark.parametrize(
    ("current", "signal", "expected"), [(*key, value) for key, value in ALLOWED.items()]
)
def test_authoritative_matrix_allows_only_documented_transitions(
    current: RunState, signal: RunSignal, expected: RunState
) -> None:
    decision = _decide(RunStateMachine(), RunStateView(state=current), signal)
    assert decision.state == expected


def test_unlisted_transition_is_rejected() -> None:
    with pytest.raises(InvalidStateTransition):
        RunStateMachine().decide(
            RunStateView(state=RunState.COMPLETED), RunSignal.RESUME_REQUESTED
        )


def test_every_state_and_signal_pair_is_documented_or_rejected() -> None:
    machine = RunStateMachine()
    for state in RunState:
        for signal in RunSignal:
            view = RunStateView(state=state)
            key = (state, signal)
            if key in REQUEST_ONLY:
                assert _decide(machine, view, signal).state == state
            elif key in ALLOWED:
                assert _decide(machine, view, signal).state == ALLOWED[key]
            else:
                with pytest.raises(InvalidStateTransition):
                    _decide(machine, view, signal)
