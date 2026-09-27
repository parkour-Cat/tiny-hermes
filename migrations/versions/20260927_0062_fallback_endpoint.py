"""A Run remembers which endpoint answers it once it switched (§7.4.1, v2.12).

`runs.answering_endpoint_id` is `NULL` until a round's main endpoint could not
be reached and a fallback answered; from then on the Run stays there, because
a thinking endpoint wants this turn's reasoning back with its tool calls and
the fallback's calls carry none. A column rather than read back from events:
events are pruned by age, and the Run must not quietly return to an endpoint
that would refuse it.

`run_events.event_type` gains `model_fallback_used` and
`model_fallback_skipped`. The downgrade deletes the rows it can no longer
allow, the same bargain every earlier widening of this CHECK made.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0062"
down_revision: str | None = "20260927_0061"
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
    "'sandbox_cache_reset', 'model_round_retried', 'model_round', 'tool_called'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'context_compaction_skipped', 'context_summary_billed', "
    "'skill_loaded', 'skill_proposed', 'http_call_refused', "
    "'tool_schema_budget_exceeded', 'mcp_tools_revalidated', "
    "'memory_proposed', 'memory_written', 'run_delegated', "
    "'sandbox_cache_reset', 'model_round_retried', 'model_round', 'tool_called', "
    "'model_fallback_used', 'model_fallback_skipped'"
)


def _clause(fixed: str) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in SIGNALS)
    fact_names = "".join(f", '{name}'" for name in WORKSPACE_FACTS)
    return f"event_type IN ({fixed}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("answering_endpoint_id", sa.Uuid(), nullable=True),
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_AFTER)
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM run_events "
        "WHERE event_type IN ('model_fallback_used', 'model_fallback_skipped')"
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )
    op.drop_column("runs", "answering_endpoint_id")
