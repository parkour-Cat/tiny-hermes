from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.runs.domain.models import (
    CallerType,
    CheckpointEffectStatus,
    PauseReason,
    RunEventType,
    RunState,
    SessionMode,
    WaitPolicy,
)
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


def _in_enum(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


def _now() -> datetime:
    return datetime.now(UTC)


class SessionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_sessions_id_workspace"),
        CheckConstraint(_in_enum("session_mode", SessionMode), name="ck_sessions_session_mode"),
        CheckConstraint(_in_enum("caller_type", CallerType), name="ck_sessions_caller_type"),
        CheckConstraint("next_run_sequence > 0", name="ck_sessions_next_run_sequence"),
        CheckConstraint(
            "next_message_sequence > 0", name="ck_sessions_next_message_sequence"
        ),
        ForeignKeyConstraint(
            ["agent_id", "workspace_id"],
            ["agents.id", "agents.workspace_id"],
            name="fk_sessions_agent",
        ),
        ForeignKeyConstraint(
            ["head_run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_sessions_head_run",
            use_alter=True,
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(index=True)
    session_mode: Mapped[str] = mapped_column(String(32), default=SessionMode.PERSISTENT.value)
    caller_type: Mapped[str] = mapped_column(String(32))
    caller_id: Mapped[UUID] = mapped_column()
    head_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    next_run_sequence: Mapped[int] = mapped_column(Integer, default=1)
    next_message_sequence: Mapped[int] = mapped_column(Integer, default=1)
    workspace_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class SessionMessageRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "session_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_session_messages_sequence"),
        CheckConstraint("sequence > 0", name="ck_session_messages_sequence_positive"),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_session_messages_session",
            ondelete="CASCADE",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(index=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    redacted: Mapped[bool] = mapped_column(default=False)


class RunRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_runs_id_workspace"),
        UniqueConstraint("session_id", "session_sequence", name="uq_runs_session_sequence"),
        CheckConstraint(_in_enum("status", RunState), name="ck_runs_status"),
        CheckConstraint(
            f"pause_reason IS NULL OR {_in_enum('pause_reason', PauseReason)}",
            name="ck_runs_pause_reason",
        ),
        CheckConstraint(
            f"wait_policy IS NULL OR {_in_enum('wait_policy', WaitPolicy)}",
            name="ck_runs_wait_policy",
        ),
        CheckConstraint(
            _in_enum("checkpoint_effect_status", CheckpointEffectStatus),
            name="ck_runs_checkpoint_effect_status",
        ),
        CheckConstraint("state_version > 0", name="ck_runs_state_version_positive"),
        CheckConstraint("recovery_attempts >= 0", name="ck_runs_recovery_attempts"),
        CheckConstraint("next_event_sequence > 0", name="ck_runs_next_event_sequence"),
        CheckConstraint("session_sequence > 0", name="ck_runs_session_sequence_positive"),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_runs_session",
        ),
        ForeignKeyConstraint(
            ["agent_version_id", "workspace_id"],
            ["agent_versions.id", "agent_versions.workspace_id"],
            name="fk_runs_agent_version",
        ),
        ForeignKeyConstraint(
            ["blocked_by_run_id"], ["runs.id"], name="fk_runs_blocked_by"
        ),
        ForeignKeyConstraint(["retry_of_run_id"], ["runs.id"], name="fk_runs_retry_of"),
        ForeignKeyConstraint(
            ["budget_root_run_id"], ["runs.id"], name="fk_runs_budget_root"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column(index=True)
    agent_version_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(32), default=RunState.QUEUED.value, index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    next_event_sequence: Mapped[int] = mapped_column(Integer, default=1)
    session_sequence: Mapped[int] = mapped_column(Integer)
    blocked_by_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pause_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    wait_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wait_policy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    wait_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_of_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    budget_root_run_id: Mapped[UUID] = mapped_column(index=True)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint_replay_safe: Mapped[bool] = mapped_column(default=True)
    checkpoint_effect_status: Mapped[str] = mapped_column(
        String(16), default=CheckpointEffectStatus.NONE.value
    )
    checkpoint_workspace_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    recovery_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunBudgetScopeRow(Base):
    __tablename__ = "run_budget_scopes"
    __table_args__ = (
        CheckConstraint(
            "max_tokens IS NULL OR max_tokens >= 0", name="ck_run_budget_scopes_max_tokens"
        ),
        CheckConstraint("version > 0", name="ck_run_budget_scopes_version_positive"),
        CheckConstraint(
            "derived_retry_count >= 0", name="ck_run_budget_scopes_retry_count"
        ),
    )

    root_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    max_execution_seconds: Mapped[int] = mapped_column(Integer)
    consumed_execution_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    max_elapsed_seconds: Mapped[int] = mapped_column(Integer)
    elapsed_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_model_calls: Mapped[int] = mapped_column(Integer)
    consumed_model_calls: Mapped[int] = mapped_column(Integer, default=0)
    max_tool_calls: Mapped[int] = mapped_column(Integer)
    consumed_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    max_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    consumed_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    max_derived_retries: Mapped[int] = mapped_column(Integer)
    derived_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class RunEventRow(IdMixin, Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
        CheckConstraint("sequence > 0", name="ck_run_events_sequence_positive"),
        CheckConstraint(_in_enum("event_type", RunEventType), name="ck_run_events_event_type"),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_run_events_run",
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(index=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WorkerLeaseRow(IdMixin, Base):
    __tablename__ = "worker_leases"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_worker_leases_run"),
        CheckConstraint("version > 0", name="ck_worker_leases_version_positive"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    worker_id: Mapped[str] = mapped_column(String(120))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)


class IdempotencyRecordRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "caller_type",
            "caller_id",
            "endpoint",
            "idempotency_key",
            name="uq_idempotency_records_scope",
        ),
        CheckConstraint(
            _in_enum("caller_type", CallerType), name="ck_idempotency_records_caller_type"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    caller_type: Mapped[str] = mapped_column(String(32))
    caller_id: Mapped[UUID] = mapped_column()
    endpoint: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
