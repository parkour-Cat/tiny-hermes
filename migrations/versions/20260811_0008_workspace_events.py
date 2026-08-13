"""Workspace facts and the limit-cleanup signal join the event vocabulary.

`run_events.event_type` is constrained to the names the platform knows —
adding one therefore needs a migration, and this is it, the same shape 0006
had. Six workspace facts (design §8/§9) and the one signal-derived name for
the Scheduler's `interrupted -> paused(limit)` confirmation.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_0008"
down_revision: str | None = "20260811_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNALS_BEFORE = (
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
)
SIGNALS_AFTER = (*SIGNALS_BEFORE, "limit_cleanup_confirmed")

WORKSPACE_FACTS = (
    "workspace_limit_exceeded",
    "workspace_conflict",
    "workspace_checkpoint_failed",
    "workspace_storage_unavailable",
    "workspace_integrity_failed",
    "workspace_entry_not_supported",
)

FIXED = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'sandbox_cache_reset'"
)


def _clause(signals: tuple[str, ...], facts: tuple[str, ...]) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in signals)
    fact_names = "".join(f", '{name}'" for name in facts)
    return f"event_type IN ({FIXED}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type",
        "run_events",
        _clause(SIGNALS_AFTER, WORKSPACE_FACTS),
    )


def downgrade() -> None:
    # Rows carrying the new types would fail the narrower constraint. Losing a
    # workspace fact on a downgrade is acceptable the way losing a state
    # transition would not be: each records what happened to files whose
    # revisions this downgrade does not touch.
    names = ", ".join(
        f"'{name}'" for name in (*WORKSPACE_FACTS, "run_limit_cleanup_confirmed")
    )
    # Literals from the tuples above, never caller input — the same shape 0006
    # used for the same reason.
    op.execute(f"DELETE FROM run_events WHERE event_type IN ({names})")  # noqa: S608
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(SIGNALS_BEFORE, ())
    )
