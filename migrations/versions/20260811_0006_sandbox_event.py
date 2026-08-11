"""The sandbox cache reset event.

`run_events.event_type` is constrained to the names the platform knows, which
is what stops a typo becoming a permanent row nothing can render. Adding one
therefore needs a migration, and this is it.

Technical design §11.3 requires the Agent be told when it starts on a fresh
writable layer. This event is the half a *person* reads; the protected runtime
hint is the half the model reads.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_0006"
down_revision: str | None = "20260811_0005"
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
)
SIGNAL_EVENT_TYPES = ", ".join(f"'run_{name}'" for name in SIGNALS)

BEFORE = (
    "event_type IN ('run_created', 'run_retry_derived', 'session_head_repaired', "
    f"'run_limit_reached', {SIGNAL_EVENT_TYPES})"
)
AFTER = (
    "event_type IN ('run_created', 'run_retry_derived', 'session_head_repaired', "
    f"'run_limit_reached', 'sandbox_cache_reset', {SIGNAL_EVENT_TYPES})"
)


def upgrade() -> None:
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", AFTER)


def downgrade() -> None:
    # A row carrying the new type would fail the narrower constraint, so it is
    # removed first. Losing a cache-reset event on a downgrade is acceptable in
    # a way that losing a state transition would not be: it records a fact
    # about a container that no longer exists.
    op.execute("DELETE FROM run_events WHERE event_type = 'sandbox_cache_reset'")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", BEFORE)
