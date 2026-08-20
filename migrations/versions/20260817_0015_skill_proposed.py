"""`run_events.event_type` gains `skill_proposed`.

0014 widened this CHECK for `skill_loaded` and said why it did so ahead of the
producer: an allowed value nobody writes is inert, and each widening costs a
window. This one arrives with its producer instead, because §7's governance
path was not designed when 0014 was written — an Agent may now suggest a change
to the material it was given, and a suggestion that left no mark on the Run
that made it is one a reviewer has to take on faith.

The downgrade deletes the rows it can no longer allow. That is the same
bargain 0014 made: a CHECK cannot be narrowed around rows that violate it, and
the alternative is a downgrade that fails on any database where the feature was
actually used. The proposals themselves are untouched — they live in
`skill_proposals` and outlive the events that point at them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260817_0015"
down_revision: str | None = "20260817_0014"
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
    "'skill_loaded', 'sandbox_cache_reset'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'skill_loaded', 'skill_proposed', 'sandbox_cache_reset'"
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
    op.execute("DELETE FROM run_events WHERE event_type IN ('skill_proposed')")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )
