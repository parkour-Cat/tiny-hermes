"""A Run may have been delegated, and the schema is where that stops at one level.

Product design §13. Three columns and two constraints, and the constraints are
the reason this migration exists rather than a place to put three columns.

**`depth` is checked, not remembered.** §13's third clause is that a child
Agent may not create a grandchild. The creation path refuses one, and a refusal
in a code path is a rule somebody can forget on the day they add a second way
to delegate. `ck_runs_depth` is the other kind of guarantee: a grandchild is a
row this database will not hold, whatever asked for it.

**`delegation_scope` is a snapshot.** It carries the six faces the child
actually got, computed once when it was created, rather than a reference to the
parent's Version. A parent republished — or rolled back — while its child is
still running would otherwise change that child's permissions mid-flight, which
is exactly the failure §16.3 stores an approval's hash to avoid. The cost is a
JSON document repeated per child; what it buys is that "what was this Run
allowed to do" stays answerable afterwards.

**The three columns move together.** `ck_runs_delegation_complete` says a Run
either has a parent, sits at depth 1 and carries a scope, or has none of the
three. A row with a parent and no scope would be a child nobody can state the
permissions of, and that is not a state worth being able to represent.

No budget column here on purpose. A child shares its parent's root through the
`budget_root_run_id` that has been on this table since §12.4, so one tree is one
budget without anything new to keep in step.

The event CHECK widens in the same migration, and for the reason the last four
widenings arrived with their producers: `run_delegated` records which children a
Run started, and a column that could hold a child Run without an event naming it
would leave the parent's timeline silent about the only thing that happened.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0027"
down_revision: str | None = "20260819_0026"
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
    "'memory_proposed', 'memory_written', "
    "'sandbox_cache_reset'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
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
    op.add_column("runs", sa.Column("parent_run_id", sa.Uuid(), nullable=True))
    # Existing Runs are Runs nobody delegated, which is what depth 0 says. The
    # server default carries them across without a data step, and stays on the
    # column so a future insert that forgets the field lands on the truth
    # rather than on null.
    op.add_column(
        "runs",
        sa.Column("depth", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("runs", sa.Column("delegation_scope", sa.JSON(), nullable=True))
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"], unique=False)
    op.create_foreign_key("fk_runs_parent", "runs", "runs", ["parent_run_id"], ["id"])
    op.create_check_constraint("ck_runs_depth", "runs", "depth >= 0 AND depth <= 1")
    op.create_check_constraint(
        "ck_runs_delegation_complete",
        "runs",
        "(parent_run_id IS NULL) = (depth = 0) AND "
        "(parent_run_id IS NULL) = (delegation_scope IS NULL)",
    )
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_AFTER)
    )


def downgrade() -> None:
    op.execute("DELETE FROM run_events WHERE event_type = 'run_delegated'")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )
    # The child Runs go with the columns. Unlike the event-type downgrades,
    # which delete rows they can no longer name, these rows would survive as
    # Runs whose parentage silently vanished — and a child Run reading as a
    # Run somebody asked for directly is worse than one that is gone.
    op.execute("DELETE FROM runs WHERE parent_run_id IS NOT NULL")
    op.drop_constraint("ck_runs_delegation_complete", "runs", type_="check")
    op.drop_constraint("ck_runs_depth", "runs", type_="check")
    op.drop_constraint("fk_runs_parent", "runs", type_="foreignkey")
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    op.drop_column("runs", "delegation_scope")
    op.drop_column("runs", "depth")
    op.drop_column("runs", "parent_run_id")
