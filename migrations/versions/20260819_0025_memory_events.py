"""`run_events.event_type` gains the two facts a memory candidate produces.

The sixth widening of this CHECK, and like the last three it arrives with its
producer. §14.1 lets a Run propose a memory, and what the workspace's policy
did with the candidate is a fact about that Run: `memory_proposed` when it was
queued for a person, `memory_written` when the rule check wrote a low-risk
private one without asking. A refused candidate leaves no event, the same
bargain the `off` policy strikes with the row it does not write.

The downgrade deletes the rows it can no longer allow, the same bargain 0014,
0015, 0019 and 0022 made.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260819_0025"
down_revision: str | None = "20260819_0024"
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
    "'skill_loaded', 'skill_proposed', 'http_call_refused', "
    "'tool_schema_budget_exceeded', 'mcp_tools_revalidated', "
    "'sandbox_cache_reset'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'skill_loaded', 'skill_proposed', 'http_call_refused', "
    "'tool_schema_budget_exceeded', 'mcp_tools_revalidated', "
    "'memory_proposed', 'memory_written', "
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
        "DELETE FROM run_events WHERE event_type IN "
        "('memory_proposed', 'memory_written')"
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )
