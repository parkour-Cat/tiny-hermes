"""`run_steers`: what an end user added to a Run still at work (§12.1, v2.12).

Held apart from the transcript until the Worker writes it in at a round
boundary; one never written in is handed back with the finished Run.
`run_events.event_type` gains `run_steered`. The downgrade deletes those
rows, the same bargain every earlier widening of that CHECK made.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0065"
down_revision: str | None = "20260927_0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNALS = (
    "lease_acquired",
    "slice_ended",
    "pause_requested",
    "resume_requested",
    "cancel_requested",
    "safe_pause_reached",
    "safe_cancel_started",
    "safe_cancel_finished",
    "approval_requested",
    "approval_approved",
    "approval_paused",
    "external_wait_started",
    "external_ready",
    "external_paused",
    "completed",
    "failed",
    "interrupted",
    "recovery_approved",
    "recovery_failed",
    "limit_cleanup_confirmed",
)

WORKSPACE_FACTS = (
    "workspace_limit_exceeded",
    "workspace_conflict",
    "workspace_checkpoint_failed",
    "workspace_storage_unavailable",
    "workspace_integrity_failed",
    "workspace_entry_not_supported",
)

FIXED_BEFORE = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'context_compaction_skipped', 'context_summary_billed', "
    "'skill_loaded', 'skill_proposed', 'http_call_refused', "
    "'tool_schema_budget_exceeded', 'mcp_tools_revalidated', "
    "'memory_proposed', 'memory_written', 'run_delegated', "
    "'sandbox_cache_reset', 'model_round_retried', 'model_round', 'tool_called', "
    "'model_fallback_used', 'model_fallback_skipped', 'todo_updated', 'skill_review'"
)
FIXED_AFTER = FIXED_BEFORE + ", 'run_steered'"


def _clause(fixed: str) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in SIGNALS)
    fact_names = "".join(f", '{name}'" for name in WORKSPACE_FACTS)
    return f"event_type IN ({fixed}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.create_table(
        "run_steers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absorbed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_run_steers_run_pending", "run_steers", ["run_id", "absorbed_at"])
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", _clause(FIXED_AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM run_events WHERE event_type = 'run_steered'")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE))
    op.drop_index("ix_run_steers_run_pending", table_name="run_steers")
    op.drop_table("run_steers")
