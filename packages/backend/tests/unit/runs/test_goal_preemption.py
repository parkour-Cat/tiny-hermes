"""判为 continue 的一轮，后面有人在等时该让位。

让位的理由是产品的：用户在任务跑到一半时说的话，几乎总是「我要改主意」或
「这不对」，让他等到任务跑完才被听见，是把机器的进度排在人的意图前面。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tiny_hermes.runs.domain.goal import GoalOutcome, GoalVerdict
from tiny_hermes.runs.domain.models import (
    BudgetSummary,
    CheckpointEffectStatus,
    PauseReason,
    QueueStatus,
    RunSignal,
    RunSnapshot,
    RunState,
    WaitPolicy,
)
from tiny_hermes.runs.domain.slice_policy import RoundOutcome, decide_after_round
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalVerdict
from tiny_hermes.runs.ports.children import DelegationWait


def _continuing(
    *,
    user_waiting: bool,
    cancel_requested: bool = False,
    pause_requested: bool = False,
    budget_allows: bool = True,
    approval: ApprovalCheck | None = None,
    delegated: DelegationWait | None = None,
    compat_window_expired: bool = False,
    outcome: GoalOutcome = GoalOutcome.CONTINUE,
    wait_seconds: int | None = None,
) -> RoundOutcome:
    """一轮判为 continue（或指定的其它裁决）的结果，除了要试的那一项之外
    什么都不拦着它继续。

    默认值全部设成「不会让 Run 停下来」的那一侧，这样任何一条测试变红时，
    红的原因只能是它自己改的那一项。
    """
    return RoundOutcome(
        verdict=GoalVerdict(
            outcome=outcome, unmet=(), instruction="继续", wait_seconds=wait_seconds
        ),
        approval=approval,
        delegated=delegated,
        cancel_requested=cancel_requested,
        pause_requested=pause_requested,
        budget_allows=budget_allows,
        slice_expired=False,
        compat_window_expired=compat_window_expired,
        user_waiting=user_waiting,
    )


def test_continue_with_somebody_waiting_completes_the_run() -> None:
    decision = decide_after_round(_continuing(user_waiting=True))

    assert decision.signal is RunSignal.COMPLETED


def test_continue_with_nobody_waiting_keeps_going() -> None:
    decision = decide_after_round(_continuing(user_waiting=False))

    assert decision.signal is not RunSignal.COMPLETED


def test_a_cancellation_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(_continuing(user_waiting=True, cancel_requested=True))

    assert decision.signal is RunSignal.SAFE_CANCEL_STARTED


def test_a_pause_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(_continuing(user_waiting=True, pause_requested=True))

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED


def test_a_done_verdict_is_not_turned_into_a_preemption() -> None:
    # `done` 已经决定了 Run 的去向，让位只接管本来会继续的那一种。
    decision = decide_after_round(
        _continuing(user_waiting=True, outcome=GoalOutcome.DONE)
    )

    assert decision.signal is RunSignal.COMPLETED


def test_an_exhausted_budget_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(_continuing(user_waiting=True, budget_allows=False))

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.LIMIT


def test_a_pending_approval_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(
        _continuing(
            user_waiting=True,
            approval=ApprovalCheck(
                verdict=ApprovalVerdict.REQUESTED,
                # Real wall-clock time, not the fixed `now` the budget/snapshot
                # fixtures use below: `_awaiting` computes `remaining` against
                # `datetime.now(UTC)`, so a fixed past timestamp here would
                # read as an already-expired approval and hit the wrong branch.
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        )
    )

    assert decision.signal is RunSignal.APPROVAL_REQUESTED


def test_a_fresh_delegation_still_outranks_a_waiting_message() -> None:
    decision = decide_after_round(
        _continuing(
            user_waiting=True,
            delegated=DelegationWait(
                child_run_ids=(uuid4(),), policy=WaitPolicy.ALL, seconds=60
            ),
        )
    )

    assert decision.signal is RunSignal.EXTERNAL_WAIT_STARTED


def test_a_failed_verdict_is_not_turned_into_a_preemption() -> None:
    # A round that broke is reported as broken, not as one somebody cut short.
    decision = decide_after_round(
        _continuing(user_waiting=True, outcome=GoalOutcome.FAILED)
    )

    assert decision.signal is RunSignal.FAILED


def test_an_undecidable_verdict_is_not_turned_into_a_preemption() -> None:
    decision = decide_after_round(
        _continuing(user_waiting=True, outcome=GoalOutcome.UNDECIDABLE)
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.OPERATOR


def test_a_wait_verdict_is_not_turned_into_a_preemption() -> None:
    decision = decide_after_round(
        _continuing(user_waiting=True, outcome=GoalOutcome.WAIT, wait_seconds=30)
    )

    assert decision.signal is RunSignal.EXTERNAL_WAIT_STARTED


def test_the_compat_window_still_outranks_a_waiting_message() -> None:
    """MINOR 3: a `chat_completions` caller blocked on its own sync deadline
    needs `pause_reason is COMPAT_TIMEOUT` to tell that pause apart from
    every other one (`completions.py`'s `_unfinished`). A message arriving
    for the *next* Run does not get to turn that report into a `completed`
    instead.
    """
    decision = decide_after_round(
        _continuing(user_waiting=True, compat_window_expired=True)
    )

    assert decision.signal is RunSignal.SAFE_PAUSE_REACHED
    assert decision.pause_reason is PauseReason.COMPAT_TIMEOUT


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


def test_the_goal_document_says_it_was_preempted() -> None:
    document = _snapshot(goal_preempted=True).document()

    assert document["goal"]["preempted"] is True


def test_an_ordinary_run_says_it_was_not() -> None:
    document = _snapshot(goal_preempted=False).document()

    assert document["goal"]["preempted"] is False
