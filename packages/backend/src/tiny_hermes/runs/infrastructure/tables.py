from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.model_catalog.domain.pricing import CostQuality
from tiny_hermes.runs.domain.approval import ApprovalStatus, ApprovalType
from tiny_hermes.runs.domain.models import (
    CallerType,
    CheckpointEffectStatus,
    DeliveryMode,
    PauseReason,
    RunEventType,
    RunState,
    SessionMode,
    WaitPolicy,
    WorkspaceCleanupTarget,
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
        Index("ix_session_messages_search", "search", postgresql_using="gin"),
    )

    session_id: Mapped[UUID] = mapped_column(index=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: §14.3's index. Generated from the message's text parts rather than from
    #: the whole document, so a search matches what was said and not the shape
    #: it was stored in. `simple` for the reason the memory index uses it: this
    #: platform serves Chinese and English side by side, and a stemmer for one
    #: mangles the other.
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', "
            "jsonb_path_query_array(content::jsonb, '$.parts[*].text')::text)",
            persisted=True,
        ),
    )
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
        CheckConstraint(
            "workspace_cleanup_target IS NULL OR "
            f"{_in_enum('workspace_cleanup_target', WorkspaceCleanupTarget)}",
            name="ck_runs_workspace_cleanup_target",
        ),
        CheckConstraint("state_version > 0", name="ck_runs_state_version_positive"),
        CheckConstraint("recovery_attempts >= 0", name="ck_runs_recovery_attempts"),
        CheckConstraint("next_event_sequence > 0", name="ck_runs_next_event_sequence"),
        CheckConstraint("session_sequence > 0", name="ck_runs_session_sequence_positive"),
        CheckConstraint(
            f"delivery_mode IS NULL OR {_in_enum('delivery_mode', DeliveryMode)}",
            name="ck_runs_delivery_mode",
        ),
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
        ForeignKeyConstraint(["parent_run_id"], ["runs.id"], name="fk_runs_parent"),
        Index(
            "ix_runs_undelivered_children",
            "parent_run_id",
            postgresql_where=text("result_delivered_at IS NULL"),
        ),
        # §13's third clause, in the schema rather than in a code path. A child
        # Agent may not create a grandchild, and the creation path refuses one
        # — but a refusal somebody can forget to write is a different guarantee
        # from a row the database will not hold.
        CheckConstraint("depth >= 0 AND depth <= 1", name="ck_runs_depth"),
        # Only a delegated Run reports to anybody, and nothing can be delivered
        # that was never produced. Both halves: a delivery stamp without a
        # result would be a parent told something nobody wrote.
        CheckConstraint(
            "(delegation_result IS NULL OR parent_run_id IS NOT NULL) AND "
            "(result_delivered_at IS NULL OR delegation_result IS NOT NULL)",
            name="ck_runs_delegation_result",
        ),
        # A delegated Run is one with a parent, at depth 1, holding the scope
        # it was granted. All three or none of them: a Run with a parent and no
        # scope would be a child nobody can say the permissions of.
        CheckConstraint(
            "(parent_run_id IS NULL) = (depth = 0) AND "
            "(parent_run_id IS NULL) = (delegation_scope IS NULL)",
            name="ck_runs_delegation_complete",
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
    # Design §6.3: where the Run must go after its sandbox and volume are
    # confirmed gone. Cleared only in the same transition that reaches the
    # target.
    workspace_cleanup_target: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    workspace_cleanup_sandbox_id: Mapped[UUID | None] = mapped_column(nullable=True)
    #: Set only when Chat Completions created the Run. The Worker holds the
    #: lease across ordinary slice boundaries until ``sync_timeout_seconds``.
    delivery_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Who may answer a `user_confirmation` for this Run, and nobody else
    #: (§16.3). Set to the caller for a `caller_type=user` Run and to the real
    #: `end_users.id` for a `caller_type=end_user` one; left null for a
    #: ServiceAccount's, which therefore has no EndUser to confirm anything —
    #: that is the section's requirement rather than a gap in it.
    #:
    #: No FK, unlike its first version (migration 0032 drops
    #: `fk_runs_end_user`). It used to point at `users.id` alone, which was
    #: correct while only `caller_type=user` ever set it; a `caller_type=
    #: end_user` Run needs the same column to hold an `end_users.id` instead,
    #: and one column cannot satisfy two foreign keys chosen by a row it
    #: cannot see. `sessions.caller_id` already answers this exact shape with
    #: no FK at all — a polymorphic subject reference is a `CallerType` check
    #: in code, not a constraint the schema can express — and this column
    #: follows that precedent instead of inventing a second one.
    end_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    #: The price this Run is measured at, fixed when it was created (§12.4).
    #: An administrator correcting a price afterwards does not rewrite what
    #: this Run cost, and a Run started before anybody entered one carries
    #: null — which reads as "unknown", never as "free".
    model_pricing_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    #: The Run that delegated this one, or null for a Run somebody asked for
    #: directly (§13). Null is the ordinary case and carries no meaning beyond
    #: "nobody delegated this".
    parent_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    #: How far down the delegation tree this Run sits. `0` for a Run a caller
    #: created, `1` for a child. The CHECK above is what makes §13's third
    #: clause a property of the schema rather than a rule the creation path
    #: has to remember: a grandchild cannot be written down.
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    #: The six faces this child actually holds, as they were computed when it
    #: was created. **A snapshot, never a reference to the parent's Version.**
    #: A parent republished or rolled back while its child is mid-flight would
    #: otherwise swap that child's permissions underneath it, and §16.3's
    #: approval hash is stored this way for the same reason. Null on a Run
    #: nobody delegated.
    delegation_scope: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: What this child is reporting back, written when it reached a terminal
    #: state and read by its parent when the parent is next able to take it.
    #: **The result, never the transcript** (§13's seventh clause): an outcome,
    #: a short summary and the Artifacts it was authorized to hand over.
    #:
    #: Written here rather than delivered directly because a parent is often
    #: not in a state that can take it — held by another Worker, or waiting on
    #: a sibling. A row survives that; a call does not.
    delegation_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    #: When the parent actually took it. **This is the idempotency key** for
    #: §13's ninth clause: delivery sets it in the same transaction that
    #: appends the turn, so a retry after a crash finds it already stamped and
    #: delivers nothing a second time.
    result_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApprovalRow(IdMixin, CreatedAtMixin, Base):
    """One request for a person's decision, and what became of it.

    §16.3. Two things about the shape are decisions rather than convention.

    The normalized call is stored **twice**: `content_hash` is what the
    platform compares and `document` is what the person is shown. Storing only
    the hash would mean a reviewer approving a string of hex; storing only the
    document would mean recomputing the hash on every read, and a normalization
    that changed would silently revalidate old approvals.

    `decided_by` is a plain user id with no foreign key to a role. Who was
    *allowed* to decide is checked when the decision is made and recorded in
    the audit trail; a role that changes afterwards must not rewrite the fact
    that this person decided this.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            _in_enum("approval_type", ApprovalType), name="ck_approvals_type"
        ),
        CheckConstraint(_in_enum("status", ApprovalStatus), name="ck_approvals_status"),
        CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL AND decided_by IS NULL)",
            name="ck_approvals_decision_complete",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_approvals_run",
        ),
        # One pending approval per Run. A Run is stopped while it waits, so a
        # second pending row could only come from a duplicate request — and two
        # rows a person could answer differently is a state nothing downstream
        # knows how to read.
        Index(
            "uq_approvals_pending_run",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_approvals_workspace_status", "workspace_id", "status"),
        Index(
            "ix_approvals_expiry",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    approval_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(16), default=ApprovalStatus.PENDING.value
    )
    #: The tool call by name, so a list is readable without opening every row.
    tool: Mapped[str] = mapped_column(String(128))
    #: Which call in the round this is about. Carried so a resumed Run can tell
    #: an approved call from another call the same round made.
    call_id: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    required_permission: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_approvals_requested_by")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[UUID | None] = mapped_column(nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)


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
    #: The workspace's ceiling, copied here when the Run was created so the
    #: valve a Run is measured against cannot move underneath it.
    max_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    #: What has been spent. **Null is unknown, not zero** — a Run whose
    #: endpoint has no price never gets a number here, and a ceiling that met
    #: one refuses rather than assuming it was satisfied.
    consumed_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    #: How that number was arrived at, in the same three words a person already
    #: sees beside token counts.
    cost_quality: Mapped[str] = mapped_column(
        String(16), default=CostQuality.UNKNOWN.value, server_default="unknown"
    )
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
