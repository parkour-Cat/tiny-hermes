"""A child's result is kept where it is written, and delivered exactly once.

Product design §13, ninth and tenth clauses. Two columns, and the second one is
the whole of the idempotency guarantee.

**A result is a row, not a call.** When a child reaches a terminal state its
parent is very often not in a state that can take the answer — another Worker
holds it, or it is still waiting on a sibling. Writing `delegation_result` on
the child makes the answer survive that; a delivery attempt would not. Nothing
is lost by a parent being busy, which is the failure the tenth clause is about.

**`result_delivered_at` is the idempotency key.** Delivery stamps it in the
same transaction that appends the turn to the parent's conversation, so a
retry after a crash finds it already set and delivers nothing a second time.
That is why it is a timestamp on the child rather than a flag on the parent:
the question "has this particular answer been handed over" is about one child,
and a counter on the parent could not answer it after a partial delivery.

The result is the **result**, never the transcript. §13's seventh clause: an
outcome, a short summary and the Artifacts the child was authorized to hand
over. The child's own conversation stays in the child's Session, where a person
can read it and the parent's context planner never has to trim it.

`ck_runs_delegation_result` says only a delegated Run reports to anybody and
nothing can be delivered that was never produced. A delivery stamp with no
result behind it would be a parent told something nobody wrote.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0028"
down_revision: str | None = "20260819_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("delegation_result", sa.JSON(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("result_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_runs_delegation_result",
        "runs",
        "(delegation_result IS NULL OR parent_run_id IS NOT NULL) AND "
        "(result_delivered_at IS NULL OR delegation_result IS NOT NULL)",
    )
    # The sweep that settles a parent's wait reads the children of one Run and
    # asks which are still undelivered. Without this it walks every Run in the
    # deployment on every tick.
    op.create_index(
        "ix_runs_undelivered_children",
        "runs",
        ["parent_run_id"],
        unique=False,
        postgresql_where=sa.text("result_delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runs_undelivered_children",
        table_name="runs",
        postgresql_where=sa.text("result_delivered_at IS NULL"),
    )
    op.drop_constraint("ck_runs_delegation_result", "runs", type_="check")
    op.drop_column("runs", "result_delivered_at")
    op.drop_column("runs", "delegation_result")
