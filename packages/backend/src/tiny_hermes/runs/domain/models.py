from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
)


class PauseReason(StrEnum):
    MANUAL = "manual"
    LIMIT = "limit"
    CONTEXT_OVERFLOW = "context_overflow"
    TOOL_BUDGET_EXCEEDED = "tool_budget_exceeded"
    COMPAT_TIMEOUT = "compat_timeout"
    OPERATOR = "operator"
    SYSTEM = "system"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    EXTERNAL_TIMEOUT = "external_timeout"


class RunSignal(StrEnum):
    LEASE_ACQUIRED = "lease_acquired"
    SLICE_ENDED = "slice_ended"
    PAUSE_REQUESTED = "pause_requested"
    RESUME_REQUESTED = "resume_requested"
    CANCEL_REQUESTED = "cancel_requested"
    SAFE_PAUSE_REACHED = "safe_pause_reached"
    SAFE_CANCEL_STARTED = "safe_cancel_started"
    SAFE_CANCEL_FINISHED = "safe_cancel_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_APPROVED = "approval_approved"
    APPROVAL_PAUSED = "approval_paused"
    EXTERNAL_WAIT_STARTED = "external_wait_started"
    EXTERNAL_READY = "external_ready"
    EXTERNAL_PAUSED = "external_paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECOVERY_APPROVED = "recovery_approved"
    RECOVERY_FAILED = "recovery_failed"


@dataclass(frozen=True)
class RunStateView:
    """Everything the state machine is allowed to look at."""

    state: RunState
    pause_reason: PauseReason | None = None
    wait_kind: str | None = None
    wait_deadline_at: datetime | None = None
    pause_requested: bool = False
    cancel_requested: bool = False
    budget_allows_execution: bool = True


@dataclass(frozen=True)
class StateDecision:
    """The single mutation a caller is allowed to apply."""

    state: RunState
    signal: RunSignal
    pause_reason: PauseReason | None = None
    wait_kind: str | None = None
    wait_deadline_at: datetime | None = None
    set_pause_requested: bool = False
    set_cancel_requested: bool = False
    clear_pause_request: bool = False
    clear_cancel_request: bool = False
    starts_execution: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
