"""Create agent catalog and run coordination tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0002"
down_revision: str | None = "20260810_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATUSES = (
    "status IN ('queued', 'running', 'waiting_approval', 'waiting_external', "
    "'paused', 'cancelling', 'interrupted', 'completed', 'failed', 'cancelled')"
)
PAUSE_REASONS = (
    "pause_reason IS NULL OR pause_reason IN ('manual', 'limit', 'context_overflow', "
    "'tool_budget_exceeded', 'compat_timeout', 'operator', 'system', 'approval_expired', "
    "'approval_rejected', 'approval_unavailable', 'external_timeout')"
)
EVENT_TYPES = (
    "event_type IN ('run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_lease_acquired', 'run_slice_ended', 'run_pause_requested', "
    "'run_resume_requested', 'run_cancel_requested', 'run_safe_pause_reached', "
    "'run_safe_cancel_started', 'run_safe_cancel_finished', 'run_approval_requested', "
    "'run_approval_approved', 'run_approval_paused', 'run_external_wait_started', "
    "'run_external_ready', 'run_external_paused', 'run_completed', 'run_failed', "
    "'run_interrupted', 'run_recovery_approved', 'run_recovery_failed')"
)


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("alias", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_agents_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_agents_id_workspace"),
        sa.UniqueConstraint("workspace_id", "alias", name="uq_agents_workspace_alias"),
    )
    op.create_index("ix_agents_workspace_id", "agents", ["workspace_id"], unique=False)

    op.create_table(
        "agent_drafts",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_agent_drafts_revision_positive"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("agent_id"),
    )

    op.create_table(
        "agent_versions",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version_number > 0", name="ck_agent_versions_number_positive"),
        sa.ForeignKeyConstraint(
            ["agent_id", "workspace_id"],
            ["agents.id", "agents.workspace_id"],
            name="fk_agent_versions_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_number"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_agent_versions_id_workspace"),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"], unique=False)
    op.create_index(
        "ix_agent_versions_workspace_id", "agent_versions", ["workspace_id"], unique=False
    )

    op.create_table(
        "sessions",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("session_mode", sa.String(length=32), nullable=False),
        sa.Column("caller_type", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.Uuid(), nullable=False),
        sa.Column("head_run_id", sa.Uuid(), nullable=True),
        sa.Column("next_run_sequence", sa.Integer(), nullable=False),
        sa.Column("next_message_sequence", sa.Integer(), nullable=False),
        sa.Column("workspace_revision_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "caller_type IN ('user', 'service_account')", name="ck_sessions_caller_type"
        ),
        sa.CheckConstraint(
            "session_mode IN ('ephemeral', 'persistent')", name="ck_sessions_session_mode"
        ),
        sa.CheckConstraint(
            "next_message_sequence > 0", name="ck_sessions_next_message_sequence"
        ),
        sa.CheckConstraint("next_run_sequence > 0", name="ck_sessions_next_run_sequence"),
        sa.ForeignKeyConstraint(
            ["agent_id", "workspace_id"],
            ["agents.id", "agents.workspace_id"],
            name="fk_sessions_agent",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_sessions_id_workspace"),
    )
    op.create_index("ix_sessions_agent_id", "sessions", ["agent_id"], unique=False)
    op.create_index("ix_sessions_workspace_id", "sessions", ["workspace_id"], unique=False)

    op.create_table(
        "runs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False),
        sa.Column("session_sequence", sa.Integer(), nullable=False),
        sa.Column("blocked_by_run_id", sa.Uuid(), nullable=True),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column("pause_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wait_kind", sa.String(length=64), nullable=True),
        sa.Column("wait_policy", sa.String(length=16), nullable=True),
        sa.Column("wait_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_of_run_id", sa.Uuid(), nullable=True),
        sa.Column("budget_root_run_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("checkpoint_replay_safe", sa.Boolean(), nullable=False),
        sa.Column("checkpoint_effect_status", sa.String(length=16), nullable=False),
        sa.Column("checkpoint_workspace_revision_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "checkpoint_effect_status IN ('none', 'confirmed', 'unknown')",
            name="ck_runs_checkpoint_effect_status",
        ),
        sa.CheckConstraint(PAUSE_REASONS, name="ck_runs_pause_reason"),
        sa.CheckConstraint(RUN_STATUSES, name="ck_runs_status"),
        sa.CheckConstraint(
            "wait_policy IS NULL OR wait_policy IN ('all', 'any')",
            name="ck_runs_wait_policy",
        ),
        sa.CheckConstraint("next_event_sequence > 0", name="ck_runs_next_event_sequence"),
        sa.CheckConstraint("session_sequence > 0", name="ck_runs_session_sequence_positive"),
        sa.CheckConstraint("state_version > 0", name="ck_runs_state_version_positive"),
        sa.ForeignKeyConstraint(
            ["agent_version_id", "workspace_id"],
            ["agent_versions.id", "agent_versions.workspace_id"],
            name="fk_runs_agent_version",
        ),
        sa.ForeignKeyConstraint(["blocked_by_run_id"], ["runs.id"], name="fk_runs_blocked_by"),
        sa.ForeignKeyConstraint(["budget_root_run_id"], ["runs.id"], name="fk_runs_budget_root"),
        sa.ForeignKeyConstraint(["retry_of_run_id"], ["runs.id"], name="fk_runs_retry_of"),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_runs_session",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_runs_id_workspace"),
        sa.UniqueConstraint("session_id", "session_sequence", name="uq_runs_session_sequence"),
    )
    op.create_index("ix_runs_agent_version_id", "runs", ["agent_version_id"], unique=False)
    op.create_index("ix_runs_budget_root_run_id", "runs", ["budget_root_run_id"], unique=False)
    op.create_index("ix_runs_session_id", "runs", ["session_id"], unique=False)
    op.create_index("ix_runs_status", "runs", ["status"], unique=False)
    op.create_index("ix_runs_workspace_id", "runs", ["workspace_id"], unique=False)

    op.create_table(
        "session_messages",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("source_run_id", sa.Uuid(), nullable=True),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_session_messages_sequence_positive"),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_session_messages_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence", name="uq_session_messages_sequence"),
    )
    op.create_index(
        "ix_session_messages_session_id", "session_messages", ["session_id"], unique=False
    )
    op.create_index(
        "ix_session_messages_workspace_id", "session_messages", ["workspace_id"], unique=False
    )

    op.create_table(
        "idempotency_records",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("caller_type", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("response_snapshot", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "caller_type IN ('user', 'service_account')",
            name="ck_idempotency_records_caller_type",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "caller_type",
            "caller_id",
            "endpoint",
            "idempotency_key",
            name="uq_idempotency_records_scope",
        ),
    )
    op.create_index(
        "ix_idempotency_records_run_id", "idempotency_records", ["run_id"], unique=False
    )
    op.create_index(
        "ix_idempotency_records_workspace_id",
        "idempotency_records",
        ["workspace_id"],
        unique=False,
    )

    op.create_table(
        "run_budget_scopes",
        sa.Column("root_run_id", sa.Uuid(), nullable=False),
        sa.Column("max_execution_seconds", sa.Integer(), nullable=False),
        sa.Column("consumed_execution_ms", sa.BigInteger(), nullable=False),
        sa.Column("max_elapsed_seconds", sa.Integer(), nullable=False),
        sa.Column("elapsed_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("consumed_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("consumed_tool_calls", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("consumed_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_derived_retries", sa.Integer(), nullable=False),
        sa.Column("derived_retry_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("derived_retry_count >= 0", name="ck_run_budget_scopes_retry_count"),
        sa.CheckConstraint(
            "max_tokens IS NULL OR max_tokens >= 0", name="ck_run_budget_scopes_max_tokens"
        ),
        sa.CheckConstraint("version > 0", name="ck_run_budget_scopes_version_positive"),
        sa.ForeignKeyConstraint(["root_run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("root_run_id"),
    )

    op.create_table(
        "run_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(EVENT_TYPES, name="ck_run_events_event_type"),
        sa.CheckConstraint("sequence > 0", name="ck_run_events_sequence_positive"),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_run_events_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"], unique=False)
    op.create_index("ix_run_events_workspace_id", "run_events", ["workspace_id"], unique=False)

    op.create_table(
        "worker_leases",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(length=120), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_worker_leases_version_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_worker_leases_run"),
    )

    # Circular ownership pointers: both targets exist only now, so they are added
    # by ALTER and dropped first on the way back down.
    op.create_foreign_key(
        "fk_agents_current_version",
        "agents",
        "agent_versions",
        ["current_version_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_sessions_head_run",
        "sessions",
        "runs",
        ["head_run_id", "workspace_id"],
        ["id", "workspace_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_sessions_head_run", "sessions", type_="foreignkey")
    op.drop_constraint("fk_agents_current_version", "agents", type_="foreignkey")

    op.drop_table("worker_leases")
    op.drop_index("ix_run_events_workspace_id", table_name="run_events")
    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_table("run_budget_scopes")
    op.drop_index("ix_idempotency_records_workspace_id", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_run_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_session_messages_workspace_id", table_name="session_messages")
    op.drop_index("ix_session_messages_session_id", table_name="session_messages")
    op.drop_table("session_messages")
    op.drop_index("ix_runs_workspace_id", table_name="runs")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_session_id", table_name="runs")
    op.drop_index("ix_runs_budget_root_run_id", table_name="runs")
    op.drop_index("ix_runs_agent_version_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_sessions_workspace_id", table_name="sessions")
    op.drop_index("ix_sessions_agent_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_agent_versions_workspace_id", table_name="agent_versions")
    op.drop_index("ix_agent_versions_agent_id", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_table("agent_drafts")
    op.drop_index("ix_agents_workspace_id", table_name="agents")
    op.drop_table("agents")
