"""Add run execution columns and the safety-valve event type."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0003"
down_revision: str | None = "20260810_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNAL_EVENT_TYPES = (
    "'run_lease_acquired', 'run_slice_ended', 'run_pause_requested', "
    "'run_resume_requested', 'run_cancel_requested', 'run_safe_pause_reached', "
    "'run_safe_cancel_started', 'run_safe_cancel_finished', 'run_approval_requested', "
    "'run_approval_approved', 'run_approval_paused', 'run_external_wait_started', "
    "'run_external_ready', 'run_external_paused', 'run_completed', 'run_failed', "
    "'run_interrupted', 'run_recovery_approved', 'run_recovery_failed'"
)

# Phase 2A vocabulary, restored verbatim on downgrade.
PREVIOUS_EVENT_TYPES = (
    "event_type IN ('run_created', 'run_retry_derived', 'session_head_repaired', "
    f"{SIGNAL_EVENT_TYPES})"
)

# Phase 2B adds the one name that is not derived from a RunSignal.
EVENT_TYPES = (
    "event_type IN ('run_created', 'run_retry_derived', 'session_head_repaired', "
    f"'run_limit_reached', {SIGNAL_EVENT_TYPES})"
)


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "recovery_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "runs",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_runs_recovery_attempts", "runs", "recovery_attempts >= 0"
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", EVENT_TYPES)


def downgrade() -> None:
    # Safe in this direction because no phase-2A row can carry the new type.
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", PREVIOUS_EVENT_TYPES
    )
    op.drop_constraint("ck_runs_recovery_attempts", "runs", type_="check")
    op.drop_column("runs", "last_heartbeat_at")
    op.drop_column("runs", "recovery_attempts")
