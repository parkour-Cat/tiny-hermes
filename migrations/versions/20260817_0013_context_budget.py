"""The context budget: what an endpoint declares, and what a Run records.

Two changes in one revision because they are two halves of one slice and each
alone needs a window: `model_endpoints` gains the declarations the budget
planner reads (product design §7.4.2 requires the accounting be computed from
the endpoint rather than guessed from a provider name), and
`run_events.event_type` gains the two names a trim and a compaction are written
under — the same CHECK constraint 0006, 0008 and 0012 have each had to widen.

`context_accounting` is backfilled to `shared` rather than left null. Every
endpoint registered before this revision has a window that behaves one way or
the other, and `shared` is the reading that sends the smaller request: an
endpoint that is really `separate` gets less context than it could take, while
the opposite mistake gets a request refused.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0013"
down_revision: str | None = "20260817_0012"
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
    "'run_limit_reached', 'goal_verdict', 'sandbox_cache_reset'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'sandbox_cache_reset'"
)


def _clause(fixed: str) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in SIGNALS)
    fact_names = "".join(f", '{name}'" for name in WORKSPACE_FACTS)
    return f"event_type IN ({fixed}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.add_column(
        "model_endpoints",
        sa.Column(
            "context_accounting",
            sa.String(length=20),
            nullable=False,
            server_default="shared",
        ),
    )
    op.add_column(
        "model_endpoints", sa.Column("tokenizer", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        "ck_model_endpoints_context_accounting",
        "model_endpoints",
        "context_accounting IN ('shared', 'separate')",
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_AFTER)
    )


def downgrade() -> None:
    # A trim and a compaction describe how a round's request was assembled.
    # Dropping them loses the explanation; the Run's own history is in the
    # transitions and the transcript, and neither is touched. Literal, never
    # caller input — the shape 0008 and 0012 used.
    op.execute(
        "DELETE FROM run_events "
        "WHERE event_type IN ('context_trimmed', 'context_compacted')"
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )
    op.drop_constraint(
        "ck_model_endpoints_context_accounting", "model_endpoints", type_="check"
    )
    op.drop_column("model_endpoints", "tokenizer")
    op.drop_column("model_endpoints", "context_accounting")
