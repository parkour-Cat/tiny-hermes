import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast
from uuid import UUID


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

    # The one name that is not derived from a signal: it records the shared
    # budget safety valve required by product design v2.4 section 12.3, which
    # is a budget fact rather than a state transition. ``event_type_for`` still
    # refuses to invent names, so this member must be written explicitly.
    RUN_LIMIT_REACHED = "run_limit_reached"

    # Also not derived from a signal: it records that a slice began on a fresh
    # writable layer, which is a fact about the sandbox rather than a state
    # transition. Technical design §11.3 requires the Agent be told, and this
    # is the half a person reads.
    SANDBOX_CACHE_RESET = "sandbox_cache_reset"

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


class QueueStatus(StrEnum):
    HEAD = "head"
    PENDING = "pending"
    SESSION_BLOCKED = "session_blocked"
    TERMINAL = "terminal"


class CacheStateHint(StrEnum):
    """What the Agent is told about the writable layer it just got.

    Only ``RESET`` is ever sent. There is nothing to say about a cache that
    survived, and saying it anyway would spend context on a non-event every
    round.
    """

    RESET = "reset"


#: Prepended to the first model call of a slice that began on a fresh layer,
#: ahead of the conversation and behind the platform's own rules — so a later
#: turn cannot displace it. §11.3 requires the Agent be told; this is the half
#: the model reads.
CACHE_RESET_HINT = (
    "This execution slice started with a fresh sandbox. Any dependencies, "
    "virtual environments, background processes or build caches from earlier "
    "in this Run are gone and must be rebuilt before they are used. Files under "
    "/workspace/data are unaffected."
)


@dataclass(frozen=True)
class TextBlock:
    text: str

    def document(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True)
class ToolCallBlock:
    """The model asking for a tool, in the platform's own shape.

    ``arguments`` is a decoded object rather than the provider's JSON string:
    the adapter decodes once, at the edge, and a failure there is a failed
    round with a named reason. Carrying the string inward would mean every
    reader had to decode it and each one could decide differently.
    """

    call_id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("a tool call needs a call_id to be answered by")
        if not self.name:
            raise ValueError("a tool call needs a name")

    def document(self) -> dict[str, Any]:
        return {
            "type": "tool_call",
            "call_id": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class ToolResultBlock:
    """What came back, tied to the call that asked.

    ``failed`` is separate from ``exit_code`` because they answer different
    questions. A command that exits non-zero did what it was asked and reported
    a result; a tool that was refused, or timed out, never ran. Collapsing them
    would tell the model a refusal was a failing command.
    """

    call_id: str
    output: str
    exit_code: int
    failed: bool

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("a tool result needs the call_id it answers")

    def document(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "call_id": self.call_id,
            "output": self.output,
            "exit_code": self.exit_code,
            "failed": self.failed,
        }


Block = TextBlock | ToolCallBlock | ToolResultBlock


@dataclass(frozen=True)
class CanonicalMessage:
    """One turn of a conversation, in the shape the platform stores and sends.

    Phase 3A carried a single string and said the widening belonged to the
    slice that had a producer. Tool calls are that producer.

    The stored document already carried a ``parts`` list, so rows written
    before this change read back unchanged — asserted against a literal
    document in `test_message_blocks.py`, because that is a promise about bytes
    already in a database rather than about a shape today's code round-trips.
    """

    role: Literal["user", "assistant", "tool"]
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if not self.blocks:
            # An empty turn is not a turn. Storing one puts a row in the
            # transcript that means nothing and that every later round skips.
            raise ValueError("a message needs at least one block")

    @property
    def text(self) -> str:
        """The words, for anything showing this to a person.

        Deliberately lossy: it drops calls and results. Anything rebuilding a
        request walks `blocks` instead, which is why the provider adapter does.
        """
        return "".join(block.text for block in self.blocks if isinstance(block, TextBlock))

    def document(self) -> dict[str, Any]:
        return {"role": self.role, "parts": [block.document() for block in self.blocks]}


def message_from_document(document: dict[str, Any]) -> CanonicalMessage:
    """Read a stored row back, tolerating one written by a later version.

    An unknown block type is dropped rather than guessed at: this version
    cannot render or replay it honestly, and a placeholder would put words in
    the Agent's mouth. A row with nothing readable becomes one empty text block
    rather than raising, because a bad row is a bad row and not a reason to
    fail a Run that has nothing to do with it.
    """
    raw: Any = document.get("parts")
    blocks: list[Block] = []
    for entry in cast(list[Any], raw) if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        part = cast(dict[str, Any], entry)
        kind = part.get("type")
        if kind == "text":
            blocks.append(TextBlock(text=str(part.get("text", ""))))
        elif kind == "tool_call":
            arguments: Any = part.get("arguments")
            blocks.append(
                ToolCallBlock(
                    call_id=str(part.get("call_id", "")),
                    name=str(part.get("name", "")),
                    arguments=cast(dict[str, Any], arguments)
                    if isinstance(arguments, dict)
                    else {},
                )
            )
        elif kind == "tool_result":
            blocks.append(
                ToolResultBlock(
                    call_id=str(part.get("call_id", "")),
                    output=str(part.get("output", "")),
                    exit_code=int(part.get("exit_code") or 0),
                    failed=bool(part.get("failed")),
                )
            )

    role = str(document.get("role", "user"))
    return CanonicalMessage(
        role=role if role in ("user", "assistant", "tool") else "user",  # pyright: ignore[reportArgumentType]
        blocks=tuple(blocks) or (TextBlock(text=""),),
    )


@dataclass(frozen=True)
class CallerIdentity:
    """The stable calling subject; never an API key or rotatable credential."""

    caller_type: CallerType
    caller_id: UUID


@dataclass(frozen=True)
class RunCapabilities:
    can_control: bool
    can_retry: bool


@dataclass(frozen=True)
class BudgetSummary:
    max_execution_seconds: int
    consumed_execution_ms: int
    max_elapsed_seconds: int
    elapsed_deadline_at: datetime
    max_model_calls: int
    consumed_model_calls: int
    max_tool_calls: int
    consumed_tool_calls: int
    max_tokens: int | None
    consumed_tokens: int
    max_derived_retries: int
    derived_retry_count: int

    def allows_execution(self, now: datetime) -> bool:
        if now >= self.elapsed_deadline_at:
            return False
        if self.consumed_model_calls >= self.max_model_calls:
            return False
        if self.consumed_execution_ms >= self.max_execution_seconds * 1000:
            return False
        return not (self.max_tokens is not None and self.consumed_tokens >= self.max_tokens)

    def document(self) -> dict[str, Any]:
        return {
            "max_execution_seconds": self.max_execution_seconds,
            "consumed_execution_ms": self.consumed_execution_ms,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "elapsed_deadline_at": self.elapsed_deadline_at.isoformat(),
            "max_model_calls": self.max_model_calls,
            "consumed_model_calls": self.consumed_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "consumed_tool_calls": self.consumed_tool_calls,
            "max_tokens": self.max_tokens,
            "consumed_tokens": self.consumed_tokens,
            "max_derived_retries": self.max_derived_retries,
            "derived_retry_count": self.derived_retry_count,
        }


@dataclass(frozen=True)
class SessionSnapshot:
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    session_mode: SessionMode
    caller: CallerIdentity
    head_run_id: UUID | None
    next_run_sequence: int
    next_message_sequence: int
    workspace_revision_id: UUID | None
    created_at: datetime

    def document(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "agent_id": str(self.agent_id),
            "session_mode": self.session_mode.value,
            "caller_type": self.caller.caller_type.value,
            "caller_id": str(self.caller.caller_id),
            "head_run_id": None if self.head_run_id is None else str(self.head_run_id),
            "next_run_sequence": self.next_run_sequence,
            "next_message_sequence": self.next_message_sequence,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class RunSnapshot:
    """Everything a caller may observe about one Run."""

    id: UUID
    workspace_id: UUID
    session_id: UUID
    agent_version_id: UUID
    state: RunState
    state_version: int
    session_sequence: int
    blocked_by_run_id: UUID | None
    pause_reason: PauseReason | None
    wait_kind: str | None
    wait_deadline_at: datetime | None
    retry_of_run_id: UUID | None
    budget_root_run_id: UUID
    last_event_sequence: int
    queue_position: int
    queue_status: QueueStatus
    budget: BudgetSummary
    available_actions: tuple[str, ...]
    checkpoint_replay_safe: bool
    checkpoint_effect_status: CheckpointEffectStatus
    #: How far the last round's Token counts can be trusted. `None` before any
    #: round has run. Reported rather than left to be inferred from a zero
    #: count: "nothing was used" and "nobody counted" are different facts, and
    #: only one of them means a Token limit was meaningfully enforced.
    checkpoint_usage_quality: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    def document(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "agent_version_id": str(self.agent_version_id),
            "status": self.state.value,
            "state_version": self.state_version,
            "session_sequence": self.session_sequence,
            "blocked_by_run_id": _optional_id(self.blocked_by_run_id),
            "pause_reason": None if self.pause_reason is None else self.pause_reason.value,
            "wait_kind": self.wait_kind,
            "wait_deadline_at": _optional_time(self.wait_deadline_at),
            "retry_of_run_id": _optional_id(self.retry_of_run_id),
            "budget_root_run_id": str(self.budget_root_run_id),
            "last_event_sequence": self.last_event_sequence,
            "queue": {"position": self.queue_position, "status": self.queue_status.value},
            "budget": self.budget.document(),
            "available_actions": list(self.available_actions),
            "checkpoint_replay_safe": self.checkpoint_replay_safe,
            "checkpoint_effect_status": self.checkpoint_effect_status.value,
            "checkpoint_usage_quality": self.checkpoint_usage_quality,
            "created_at": self.created_at.isoformat(),
            "started_at": _optional_time(self.started_at),
            "finished_at": _optional_time(self.finished_at),
        }


@dataclass(frozen=True)
class RunEvent:
    id: UUID
    run_id: UUID
    sequence: int
    event_type: RunEventType
    occurred_at: datetime


def fingerprint_request(
    method: str,
    endpoint: str,
    workspace_id: UUID,
    scope_id: UUID,
    message: CanonicalMessage | None,
    limit_overrides: dict[str, Any] | None,
) -> str:
    """Hash only caller-supplied request identity.

    Server-derived values such as the Agent's current version pointer stay out,
    so a network retry still replays the original Run after a later publish.
    """
    payload = {
        "method": method.upper(),
        "endpoint": endpoint,
        "workspace_id": str(workspace_id),
        "scope_id": str(scope_id),
        "message": None if message is None else message.document(),
        "limit_overrides": limit_overrides or {},
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_id(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
