"""`run_events.event_type` gains the v2.27 events (§1.27).

One row per attachment the Worker fetched into the sandbox's inbox, so a later
slice of the same Run does not fetch it again over whatever the Agent made of
it; and one per attachment it could not, with the reason the model was told.
`file_sent` is one per file `file.send` delivered or saved for download, and
also counts a Run's sends against its ceiling.
The downgrade deletes the rows it can no longer allow, the same bargain every
earlier widening of this CHECK made.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261025_0082"
down_revision: str | None = "20261025_0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = (
    "run_created",
    "run_retry_derived",
    "session_head_repaired",
    "run_limit_reached",
    "goal_verdict",
    "context_trimmed",
    "context_compacted",
    "context_compaction_skipped",
    "model_round_retried",
    "model_round",
    "tool_called",
    "model_fallback_used",
    "model_fallback_skipped",
    "todo_updated",
    "skill_review",
    "run_steered",
    "context_summary_billed",
    "skill_loaded",
    "skill_proposed",
    "http_call_refused",
    "tool_schema_budget_exceeded",
    "mcp_tools_revalidated",
    "memory_proposed",
    "memory_written",
    "run_delegated",
    "sandbox_cache_reset",
    "run_lease_acquired",
    "run_slice_ended",
    "run_pause_requested",
    "run_resume_requested",
    "run_cancel_requested",
    "run_safe_pause_reached",
    "run_safe_cancel_started",
    "run_safe_cancel_finished",
    "run_approval_requested",
    "run_approval_approved",
    "run_approval_paused",
    "run_external_wait_started",
    "run_external_ready",
    "run_external_paused",
    "run_completed",
    "run_failed",
    "run_interrupted",
    "run_recovery_approved",
    "run_recovery_failed",
    "run_limit_cleanup_confirmed",
    "workspace_limit_exceeded",
    "workspace_conflict",
    "workspace_checkpoint_failed",
    "workspace_storage_unavailable",
    "workspace_integrity_failed",
    "workspace_entry_not_supported",
)

ADDED = (
    "attachment_placed",
    "attachment_failed",
    "file_sent",
)


def _clause(names: Sequence[str]) -> str:
    return "event_type IN (" + ", ".join(f"'{name}'" for name in names) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause((*BEFORE, *ADDED))
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM run_events "
        "WHERE event_type IN ('attachment_placed', 'attachment_failed', 'file_sent')"
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", _clause(BEFORE))
