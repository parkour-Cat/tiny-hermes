from datetime import UTC, datetime, timedelta

import pytest
from tiny_hermes.runs.domain.models import PauseReason, RunSignal, RunState, RunStateView
from tiny_hermes.runs.domain.state_machine import (
    InvalidStateMetadata,
    RunLimitReached,
    RunStateMachine,
)


def test_running_pause_sets_request_without_claiming_paused() -> None:
    decision = RunStateMachine().decide(
        RunStateView(state=RunState.RUNNING), RunSignal.PAUSE_REQUESTED
    )
    assert decision.state == RunState.RUNNING
    assert decision.set_pause_requested is True


def test_running_cancel_sets_request_without_claiming_cancelled() -> None:
    decision = RunStateMachine().decide(
        RunStateView(state=RunState.RUNNING), RunSignal.CANCEL_REQUESTED
    )
    assert decision.state == RunState.RUNNING
    assert decision.set_cancel_requested is True


def test_budget_exhaustion_removes_resume_action() -> None:
    view = RunStateView(
        state=RunState.PAUSED,
        pause_reason=PauseReason.LIMIT,
        budget_allows_execution=False,
    )
    assert RunStateMachine().available_actions(view, can_control=True, can_retry=False) == (
        "cancel",
    )


def test_queued_pause_is_always_manual() -> None:
    decision = RunStateMachine().decide(
        RunStateView(state=RunState.QUEUED), RunSignal.PAUSE_REQUESTED
    )
    assert decision.state == RunState.PAUSED
    assert decision.pause_reason is PauseReason.MANUAL


def test_paused_signals_reject_reasons_that_do_not_belong_to_them() -> None:
    machine = RunStateMachine()
    with pytest.raises(InvalidStateMetadata):
        machine.decide(
            RunStateView(state=RunState.WAITING_EXTERNAL),
            RunSignal.EXTERNAL_PAUSED,
            pause_reason=PauseReason.APPROVAL_EXPIRED,
        )
    with pytest.raises(InvalidStateMetadata):
        machine.decide(
            RunStateView(state=RunState.WAITING_APPROVAL),
            RunSignal.APPROVAL_PAUSED,
            pause_reason=PauseReason.CONTEXT_OVERFLOW,
        )
    with pytest.raises(InvalidStateMetadata):
        machine.decide(RunStateView(state=RunState.RUNNING), RunSignal.SAFE_PAUSE_REACHED)


def test_waiting_signals_require_their_metadata() -> None:
    machine = RunStateMachine()
    with pytest.raises(InvalidStateMetadata):
        machine.decide(RunStateView(state=RunState.RUNNING), RunSignal.APPROVAL_REQUESTED)
    with pytest.raises(InvalidStateMetadata):
        machine.decide(
            RunStateView(state=RunState.RUNNING), RunSignal.EXTERNAL_WAIT_STARTED
        )
    decision = machine.decide(
        RunStateView(state=RunState.RUNNING),
        RunSignal.EXTERNAL_WAIT_STARTED,
        wait_deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert decision.state == RunState.WAITING_EXTERNAL
    assert decision.wait_deadline_at is not None


def test_resume_and_claim_fail_when_the_shared_budget_is_exhausted() -> None:
    machine = RunStateMachine()
    with pytest.raises(RunLimitReached):
        machine.decide(
            RunStateView(
                state=RunState.PAUSED,
                pause_reason=PauseReason.LIMIT,
                budget_allows_execution=False,
            ),
            RunSignal.RESUME_REQUESTED,
        )
    with pytest.raises(RunLimitReached):
        machine.decide(
            RunStateView(state=RunState.QUEUED, budget_allows_execution=False),
            RunSignal.LEASE_ACQUIRED,
        )


def test_terminal_states_expose_no_actions_and_failed_exposes_retry_only_when_allowed() -> None:
    machine = RunStateMachine()
    for state in (RunState.COMPLETED, RunState.CANCELLED):
        view = RunStateView(state=state)
        assert machine.available_actions(view, can_control=True, can_retry=True) == ()
    failed = RunStateView(state=RunState.FAILED)
    assert machine.available_actions(failed, can_control=True, can_retry=False) == ()
    assert machine.available_actions(failed, can_control=True, can_retry=True) == ("retry",)


def test_viewers_receive_no_control_actions() -> None:
    machine = RunStateMachine()
    view = RunStateView(state=RunState.QUEUED)
    assert machine.available_actions(view, can_control=True, can_retry=False) == (
        "pause",
        "cancel",
    )
    assert machine.available_actions(view, can_control=False, can_retry=True) == ()
