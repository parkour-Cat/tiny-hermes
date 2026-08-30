from datetime import UTC, datetime
from uuid import uuid4

from tiny_hermes.runs.domain.models import (
    BudgetSummary,
    CheckpointEffectStatus,
    PauseReason,
    QueueStatus,
    RunSnapshot,
    RunState,
)


def _budget() -> BudgetSummary:
    return BudgetSummary(
        max_execution_seconds=900,
        consumed_execution_ms=0,
        max_elapsed_seconds=86_400,
        elapsed_deadline_at=datetime(2026, 8, 13, tzinfo=UTC),
        max_model_calls=20,
        consumed_model_calls=0,
        max_tool_calls=50,
        consumed_tool_calls=0,
        max_tokens=None,
        consumed_tokens=0,
        max_derived_retries=3,
        derived_retry_count=0,
    )


def _snapshot(**overrides: object) -> RunSnapshot:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "session_id": uuid4(),
        "agent_version_id": uuid4(),
        "state": RunState.QUEUED,
        "state_version": 1,
        "session_sequence": 1,
        "blocked_by_run_id": None,
        "pause_reason": None,
        "wait_kind": None,
        "wait_deadline_at": None,
        "retry_of_run_id": None,
        "budget_root_run_id": uuid4(),
        "parent_run_id": None,
        "depth": 0,
        "last_event_sequence": 1,
        "queue_position": 1,
        "queue_status": QueueStatus.HEAD,
        "budget": _budget(),
        "available_actions": ("pause", "cancel"),
        "checkpoint_replay_safe": True,
        "checkpoint_effect_status": CheckpointEffectStatus.NONE,
        "checkpoint_usage_quality": None,
        "failure_reason": None,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return RunSnapshot(**values)  # type: ignore[arg-type]


def test_head_pending_and_terminal_snapshots_omit_head_fields() -> None:
    head = _snapshot().document()["queue"]
    pending = _snapshot(
        queue_position=2, queue_status=QueueStatus.PENDING, session_sequence=2
    ).document()["queue"]
    terminal = _snapshot(
        state=RunState.COMPLETED,
        queue_position=0,
        queue_status=QueueStatus.TERMINAL,
        available_actions=(),
        finished_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    ).document()["queue"]

    assert head == {"position": 1, "status": "head"}
    assert pending == {"position": 2, "status": "pending"}
    assert terminal == {"position": 0, "status": "terminal"}
    for queue in (head, pending, terminal):
        assert "head_status" not in queue
        assert "head_reason" not in queue
        assert "available_actions" not in queue
        assert "blocked_by_run_id" not in queue


def test_a_blocked_snapshot_names_the_head_and_what_the_caller_can_do_to_it() -> None:
    head_id = uuid4()
    document = _snapshot(
        session_sequence=2,
        blocked_by_run_id=head_id,
        queue_position=2,
        queue_status=QueueStatus.SESSION_BLOCKED,
        available_actions=("pause", "cancel"),
        head_status=RunState.PAUSED,
        head_pause_reason=PauseReason.MANUAL,
        queue_available_actions=("resume", "cancel"),
    ).document()

    assert document["blocked_by_run_id"] == str(head_id)
    assert document["queue"] == {
        "status": "session_blocked",
        "blocked_by_run_id": str(head_id),
        "position": 2,
        "head_status": "paused",
        "head_reason": {
            "pause_reason": "manual",
            "wait_kind": None,
            "wait_deadline_at": None,
        },
        "available_actions": ["resume", "cancel"],
    }
    assert document["available_actions"] == ["pause", "cancel"]


def test_a_failed_run_says_why_in_its_own_document() -> None:
    """A caller must not have to read the transcript to learn the reason.

    Before this field the snapshot carried 22 keys and none of them was the
    verdict, while the checkpoint had held `failure` the whole time.
    """
    document = _snapshot(
        state=RunState.FAILED,
        queue_position=0,
        queue_status=QueueStatus.TERMINAL,
        failure_reason="deterministic_command_failed",
    ).document()

    assert document["status"] == "failed"
    assert document["failure_reason"] == "deterministic_command_failed"


def test_a_run_that_has_not_failed_reports_no_reason() -> None:
    assert _snapshot().document()["failure_reason"] is None


def test_a_running_run_says_which_round_it_is_on_and_why_it_continued() -> None:
    """The status of a Run in its fifth round is the same word as in its first.

    Whoever is watching wants the other two facts: how far along, and what the
    platform decided the last time it looked.
    """
    document = _snapshot(
        state=RunState.RUNNING,
        current_round=5,
        goal_outcome="continue",
        goal_unmet=("/workspace/data/report.md", "pytest -q"),
    ).document()

    assert document["goal"] == {
        "round": 5,
        "outcome": "continue",
        "unmet": ["/workspace/data/report.md", "pytest -q"],
        "preempted": False,
    }


def test_the_goal_is_reported_before_any_round_has_been_judged() -> None:
    """Present and empty rather than absent: a caller that has to branch on
    whether the key exists learns nothing the null values do not already say."""
    assert _snapshot().document()["goal"] == {
        "round": None,
        "outcome": None,
        "unmet": [],
        "preempted": False,
    }


def test_a_run_that_met_every_declared_condition_reports_none_unmet() -> None:
    """`done` with an empty list is not the same fact as no verdict at all, so
    the outcome is what tells a reader which one this is."""
    document = _snapshot(
        state=RunState.COMPLETED,
        queue_position=0,
        queue_status=QueueStatus.TERMINAL,
        current_round=3,
        goal_outcome="done",
    ).document()

    assert document["goal"] == {
        "round": 3,
        "outcome": "done",
        "unmet": [],
        "preempted": False,
    }
