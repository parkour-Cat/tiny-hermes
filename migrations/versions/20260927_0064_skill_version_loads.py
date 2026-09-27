"""`skill_version_loads`: which Runs loaded which skill version (§15.4, v2.12).

The evidence a reviewer reads. A table of its own because the `skill_loaded`
events it mirrors are pruned with their terminal Run's events after the
retention window. Backfilled from the events still present; a Run whose
events are already gone is not recoverable, and the counts start from there.

`run_events.event_type` also gains `skill_review`, the post-run review's own
event. The downgrade deletes those rows, the same bargain every earlier
widening of that CHECK made.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0064"
down_revision: str | None = "20260927_0063"
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
    "'model_fallback_used', 'model_fallback_skipped', 'todo_updated'"
)
FIXED_AFTER = FIXED_BEFORE + ", 'skill_review'"


def _clause(fixed: str) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in SIGNALS)
    fact_names = "".join(f", '{name}'" for name in WORKSPACE_FACTS)
    return f"event_type IN ({fixed}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", _clause(FIXED_AFTER))
    op.create_table(
        "skill_version_loads",
        sa.Column(
            "skill_version_id",
            sa.Uuid(),
            sa.ForeignKey("skill_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_skill_version_loads_workspace_version",
        "skill_version_loads",
        ["workspace_id", "skill_version_id"],
    )
    op.execute(
        """
        INSERT INTO skill_version_loads (skill_version_id, run_id, workspace_id, loaded_at)
        SELECT DISTINCT ON (v.id, e.run_id) v.id, e.run_id, e.workspace_id, e.occurred_at
        FROM run_events e
        JOIN skill_versions v ON v.id::text = e.payload->>'skill_version_id'
        WHERE e.event_type = 'skill_loaded'
        ORDER BY v.id, e.run_id, e.occurred_at
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM run_events WHERE event_type = 'skill_review'")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE))
    op.drop_index("ix_skill_version_loads_workspace_version", table_name="skill_version_loads")
    op.drop_table("skill_version_loads")
