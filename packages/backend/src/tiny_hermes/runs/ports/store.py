from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from tiny_hermes.runs.domain.models import (
    CallerIdentity,
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
    expected_lease_version: int
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


@dataclass(frozen=True)
class RenewedLease:
    lease_id: UUID
    version: int
    expires_at: datetime


@dataclass(frozen=True)
class ClaimRunCommand:
    workspace_id: UUID
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

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot: ...

    async def append_events(
        self, command: AppendEventsCommand
    ) -> tuple[RunEvent, ...]: ...

    async def claim_head(self, command: ClaimRunCommand) -> ClaimedRun | None: ...

    async def renew_lease(self, command: RenewLeaseCommand) -> RenewedLease | None: ...

    async def record_slice(self, command: RecordSliceCommand) -> RunSnapshot: ...

    async def repair_session_head(
        self, session_id: UUID, request_id: str
    ) -> RepairResult: ...

    async def derive_retry(self, command: RetryRunCommand) -> AcceptedRun: ...
