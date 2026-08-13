"""The one new door out of `interrupted`, and how narrow it is.

Design §9: `interrupted -> paused` exists only for a Run whose over-limit
rollback was fully recorded and whose sandbox and volume are confirmed gone.
Every other interrupted Run keeps the two exits it always had — recovery or
failure — because a Run that could sidle into `paused` without the recorded
rollback would tell its owner it is resumable when nobody knows what state
its workspace is in.
"""

import pytest
from tiny_hermes.runs.domain.models import (
    PauseReason,
    RunEventType,
    RunSignal,
    RunState,
    RunStateView,
    event_type_for,
)
from tiny_hermes.runs.domain.state_machine import (
    InvalidStateMetadata,
    InvalidStateTransition,
    RunStateMachine,
)

machine = RunStateMachine()


def test_only_a_recorded_limit_pause_may_leave_interrupted_for_paused() -> None:
    view = RunStateView(state=RunState.INTERRUPTED)

    decision = machine.decide(
        view, RunSignal.LIMIT_CLEANUP_CONFIRMED, pause_reason=PauseReason.LIMIT
    )

    assert decision.state is RunState.PAUSED
    assert decision.pause_reason is PauseReason.LIMIT


def test_the_limit_door_requires_its_reason_spelled_out() -> None:
    view = RunStateView(state=RunState.INTERRUPTED)
    with pytest.raises(InvalidStateMetadata):
        machine.decide(view, RunSignal.LIMIT_CLEANUP_CONFIRMED)
    with pytest.raises(InvalidStateMetadata):
        machine.decide(
            view, RunSignal.LIMIT_CLEANUP_CONFIRMED, pause_reason=PauseReason.MANUAL
        )


def test_no_other_interrupted_signal_reaches_paused() -> None:
    view = RunStateView(state=RunState.INTERRUPTED)
    for signal in RunSignal:
        if signal is RunSignal.LIMIT_CLEANUP_CONFIRMED:
            continue
        needs_reason = signal is RunSignal.SAFE_PAUSE_REACHED
        try:
            decision = machine.decide(
                view,
                signal,
                pause_reason=PauseReason.LIMIT if needs_reason else None,
            )
        except (InvalidStateTransition, InvalidStateMetadata):
            continue
        assert decision.state is not RunState.PAUSED, signal


def test_the_limit_door_opens_from_interrupted_and_nowhere_else() -> None:
    for state in RunState:
        if state is RunState.INTERRUPTED:
            continue
        with pytest.raises(InvalidStateTransition):
            machine.decide(
                RunStateView(state=state),
                RunSignal.LIMIT_CLEANUP_CONFIRMED,
                pause_reason=PauseReason.LIMIT,
            )


def test_the_new_signal_has_its_mechanically_derived_event_name() -> None:
    assert (
        event_type_for(RunSignal.LIMIT_CLEANUP_CONFIRMED)
        is RunEventType.RUN_LIMIT_CLEANUP_CONFIRMED
    )


def test_the_workspace_facts_have_event_names_a_person_can_grep() -> None:
    expected = {
        "workspace_limit_exceeded",
        "workspace_conflict",
        "workspace_checkpoint_failed",
        "workspace_storage_unavailable",
        "workspace_integrity_failed",
        "workspace_entry_not_supported",
    }
    assert expected <= {member.value for member in RunEventType}
