"""`run_events.event_type` gains `context_compaction_skipped`.

A compaction that would not make the context smaller is now refused instead
of applied, and refusing is not the same as failing: `CONTEXT_COMPACTED`
says a compaction happened, and its absence used to mean exactly one thing
— the summary could not be generated. That is what the receipt says
(「稍后再试一次」), and for the second case it is false: retrying costs
another summarization call and reaches the same conclusion.

So the refusal gets its own row rather than being inferred from the absence
of another. What made this necessary was a real one: the first `/compact`
this platform served in production recorded `covered 2, freed_estimate 0`
beside a `context_summary_billed` of 355 + 1,372 tokens — a summary longer
than what it replaced, applied anyway, reported as 「已压缩」.

The downgrade deletes the rows it can no longer allow, the same bargain
every earlier widening of this CHECK made.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260903_0055"
down_revision: str | None = "20260902_0054"
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
    "'context_summary_billed', "
    "'skill_loaded', 'skill_proposed', 'http_call_refused', "
    "'tool_schema_budget_exceeded', 'mcp_tools_revalidated', "
    "'memory_proposed', 'memory_written', 'run_delegated', "
    "'sandbox_cache_reset'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'context_compaction_skipped', 'context_summary_billed', "
    "'skill_loaded', 'skill_proposed', 'http_call_refused', "
    "'tool_schema_budget_exceeded', 'mcp_tools_revalidated', "
    "'memory_proposed', 'memory_written', 'run_delegated', "
    "'sandbox_cache_reset'"
)


def _clause(fixed: str) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in SIGNALS)
    fact_names = "".join(f", '{name}'" for name in WORKSPACE_FACTS)
    return f"event_type IN ({fixed}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_AFTER)
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM run_events WHERE event_type IN ('context_compaction_skipped')"
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )
