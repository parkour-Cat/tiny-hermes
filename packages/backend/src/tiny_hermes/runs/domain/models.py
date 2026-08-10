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


class SessionMode(StrEnum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


class CallerType(StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"


class CheckpointEffectStatus(StrEnum):
    NONE = "none"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class WaitPolicy(StrEnum):
    ALL = "all"
    ANY = "any"


class RunEventType(StrEnum):
    """Run Event names.

    Signal-driven names are derived mechanically from ``RunSignal`` so the
    event vocabulary can never drift away from the state matrix.
    """

    RUN_CREATED = "run_created"
    RUN_RETRY_DERIVED = "run_retry_derived"
    SESSION_HEAD_REPAIRED = "session_head_repaired"

    RUN_LEASE_ACQUIRED = "run_lease_acquired"
    RUN_SLICE_ENDED = "run_slice_ended"
    RUN_PAUSE_REQUESTED = "run_pause_requested"
    RUN_RESUME_REQUESTED = "run_resume_requested"
    RUN_CANCEL_REQUESTED = "run_cancel_requested"
    RUN_SAFE_PAUSE_REACHED = "run_safe_pause_reached"
    RUN_SAFE_CANCEL_STARTED = "run_safe_cancel_started"
    RUN_SAFE_CANCEL_FINISHED = "run_safe_cancel_finished"
    RUN_APPROVAL_REQUESTED = "run_approval_requested"
    RUN_APPROVAL_APPROVED = "run_approval_approved"
    RUN_APPROVAL_PAUSED = "run_approval_paused"
    RUN_EXTERNAL_WAIT_STARTED = "run_external_wait_started"
    RUN_EXTERNAL_READY = "run_external_ready"
    RUN_EXTERNAL_PAUSED = "run_external_paused"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_RECOVERY_APPROVED = "run_recovery_approved"
    RUN_RECOVERY_FAILED = "run_recovery_failed"


def event_type_for(signal: RunSignal) -> RunEventType:
    return RunEventType(f"run_{signal.value}")


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
