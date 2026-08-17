from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.runs.domain.models import (
    BudgetSummary,
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    CheckpointEffectStatus,
    PauseReason,
    RunCapabilities,
    RunEvent,
    RunEventType,
    RunSignal,
    RunSnapshot,
    SessionMode,
    SessionSnapshot,
    WorkspaceCleanupTarget,
)
from tiny_hermes.tenancy.domain.models import Role


@dataclass(frozen=True)
class CreateSessionCommand:
    workspace_id: UUID
    agent_id: UUID
    caller: CallerIdentity
    session_mode: SessionMode
    request_id: str


@dataclass(frozen=True)
class AcceptRunCommand:
    """Everything one idempotent Run acceptance transaction needs."""

    workspace_id: UUID
    session_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    endpoint: str
    idempotency_key: str
    request_fingerprint: str
    message: CanonicalMessage
    request_id: str
    delivery_mode: str | None = None


@dataclass(frozen=True)
class RetryRunCommand:
    workspace_id: UUID
    source_run_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    endpoint: str
    idempotency_key: str
    request_fingerprint: str
    request_id: str


@dataclass(frozen=True)
class ControlRunCommand:
    """A user control request; the state machine still chooses the mutation."""

    workspace_id: UUID
    run_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    signal: RunSignal
    expected_state_version: int
    request_id: str


@dataclass(frozen=True)
class WidenBudgetCommand:
    """Raise one ceiling on a Run's shared budget scope.

    Product design §12.3 requires an explicit act by an authorized subject, so
    this is its own command rather than a field someone can set while doing
    something else. It carries no `consumed_*` value at all: the counters are
    not this operation's business, and a widening that could reset one would be
    a way to defeat the safety valve by re-spending the same budget.
    """

    workspace_id: UUID
    run_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    expected_state_version: int
    max_model_calls: int
    request_id: str


@dataclass(frozen=True)
class ApplySignalCommand:
    """The one seam future Worker and Scheduler code uses for state signals.

    It never accepts a target state; ``RunStateMachine`` decides.
    """

    workspace_id: UUID
    run_id: UUID
    signal: RunSignal
    request_id: str
    capabilities: RunCapabilities
    expected_state_version: int | None = None
    pause_reason: PauseReason | None = None
    wait_kind: str | None = None
    wait_deadline_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    #: For `LIMIT_CLEANUP_CONFIRMED` only: the sandbox the caller confirmed
    #: gone, compared against the Run's recorded cleanup intent.
    confirmed_sandbox_id: UUID | None = None


@dataclass(frozen=True)
class ReservedEvent:
    event_type: RunEventType
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class AppendEventsCommand:
    """Reserve and write one or more redacted events in one transaction.

    Callers never choose a sequence; the allocator does.
    """

    workspace_id: UUID
    run_id: UUID
    events: tuple[ReservedEvent, ...]


@dataclass(frozen=True)
class RenewLeaseCommand:
    workspace_id: UUID
    run_id: UUID
    lease_id: UUID
    expected_version: int
    lease_seconds: int


@dataclass(frozen=True)
class RecordSliceCommand:
    """One checkpoint, its accounting, its state signal, and the lease release.

    These four are one business operation. Splitting them would let a crash
    leave a released lease beside unaccounted execution time.

    A ``signal`` of ``None`` means the Worker keeps the lease and continues to
    the next round, so only the checkpoint and the accounting are written.
    """

    workspace_id: UUID
    run_id: UUID
    lease_id: UUID
    expected_state_version: int
    signal: RunSignal | None
    pause_reason: PauseReason | None
    limit_reached: bool
    checkpoint: dict[str, Any]
    checkpoint_replay_safe: bool
    checkpoint_effect_status: CheckpointEffectStatus
    executed_ms: int
    model_calls: int
    tokens: int
    request_id: str
    capabilities: RunCapabilities
    #: Whole messages appended to the Session in this transaction: the
    #: assistant's turn, and a tool turn when the round called one. Empty for a
    #: round that failed — the transcript holds what the Agent said, never what
    #: it tried to say.
    #:
    #: Phase 3A carried a single string here, which could not express a round
    #: that acted rather than answered.
    appended: tuple[CanonicalMessage, ...] = ()
    #: Design §6.3: where the Run must go after this exact sandbox and its
    #: volume are confirmed gone. Recorded in the same transaction as the
    #: rollback results, so a crash in between is recoverable from rows.
    workspace_cleanup_target: WorkspaceCleanupTarget | None = None
    workspace_cleanup_sandbox_id: UUID | None = None
    #: Facts written with the slice's own transition (workspace rollbacks name
    #: their reason here). Only written when ``signal`` is not None — a round
    #: that keeps the lease has no transition to attach them to.
    events: tuple["ReservedEvent", ...] = ()


@dataclass(frozen=True)
class ExecutionContext:
    """What a Worker needs to run one round, read through the store.

    The Agent configuration comes from the Version the Run fixed at creation,
    never from the Agent's current pointer, so publishing mid-flight cannot
    change how an accepted Run behaves.
    """

    run_id: UUID
    state_version: int
    spec: AgentSpec
    #: The conversation this round is given, oldest first. A persistent Session
    #: hands over everything said in it so far; an ephemeral one hands over only
    #: this Run's own input, which is the first behaviour `session_mode` has
    #: ever had.
    messages: tuple[CanonicalMessage, ...]
    cancel_requested: bool
    pause_requested: bool
    budget: BudgetSummary
    #: When set, this Run was admitted by Chat Completions and the Worker must
    #: not emit ``SLICE_ENDED`` for an ordinary slice boundary before it.
    compat_deadline_at: datetime | None = None


@dataclass(frozen=True)
class RenewedLease:
    lease_id: UUID
    version: int
    expires_at: datetime


@dataclass(frozen=True)
class RunEventRecord:
    """One retained Run Event as a subscriber receives it."""

    sequence: int
    event_type: RunEventType
    occurred_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunEventWindow:
    """What a subscriber can still be given.

    ``earliest_sequence`` is ``None`` once retention removed every event, so a
    resynchronization hint has to fall back to ``next_sequence``.
    """

    earliest_sequence: int | None
    next_sequence: int
    is_terminal: bool


@dataclass(frozen=True)
class ClaimRunCommand:
    """A claim attempt.

    ``workspace_id`` of ``None`` means any workspace this Worker serves, which
    is what a deployed Worker process wants; a concrete value narrows the
    search for tests and for future per-tenant Workers.
    """

    workspace_id: UUID | None
    worker_id: str
    lease_seconds: int
    request_id: str
    capabilities: RunCapabilities
    session_id: UUID | None = None


@dataclass(frozen=True)
class AcceptedRun:
    """A created or replayed Run plus the exact document the caller receives."""

    run_id: UUID
    document: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class ClaimedRun:
    run: RunSnapshot
    lease_id: UUID
    lease_version: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class RepairResult:
    session_id: UUID
    changed: bool
    head_run_id: UUID | None


class RunStore(Protocol):
    """Run Coordination persistence.

    Every method is one explicit database transaction step. No caller may
    update Run state columns, event sequences, or Session heads directly.
    """

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_session(self, command: CreateSessionCommand) -> SessionSnapshot: ...

    async def get_session(
        self, workspace_id: UUID, session_id: UUID
    ) -> SessionSnapshot | None: ...

    async def list_sessions(self, workspace_id: UUID) -> Sequence[SessionSnapshot]: ...

    async def accept_run(self, command: AcceptRunCommand) -> AcceptedRun: ...

    async def get_run(
        self, workspace_id: UUID, run_id: UUID, capabilities: RunCapabilities
    ) -> RunSnapshot | None: ...

    async def list_runs(
        self, workspace_id: UUID, session_id: UUID | None, capabilities: RunCapabilities
    ) -> Sequence[RunSnapshot]: ...

    async def control_run(self, command: ControlRunCommand) -> RunSnapshot: ...

    async def widen_budget(self, command: WidenBudgetCommand) -> RunSnapshot: ...

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot: ...

    async def append_events(
        self, command: AppendEventsCommand
    ) -> tuple[RunEvent, ...]: ...

    async def event_window(
        self, workspace_id: UUID, run_id: UUID
    ) -> RunEventWindow | None: ...

    async def list_events_after(
        self, workspace_id: UUID, run_id: UUID, after_sequence: int, limit: int
    ) -> Sequence[RunEventRecord]: ...

    async def claim_head(self, command: ClaimRunCommand) -> ClaimedRun | None: ...

    async def execution_context(
        self, workspace_id: UUID, run_id: UUID
    ) -> ExecutionContext | None: ...

    async def renew_lease(self, command: RenewLeaseCommand) -> RenewedLease | None: ...

    async def record_slice(self, command: RecordSliceCommand) -> RunSnapshot: ...

    async def repair_session_head(
        self, session_id: UUID, request_id: str
    ) -> RepairResult: ...

    async def derive_retry(self, command: RetryRunCommand) -> AcceptedRun: ...

    async def list_session_messages(
        self, workspace_id: UUID, session_id: UUID
    ) -> Sequence[CanonicalMessage]: ...

    async def claim_idempotency(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun | None: ...

    async def store_idempotency_response(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        run_id: UUID,
        document: dict[str, Any],
    ) -> None: ...
