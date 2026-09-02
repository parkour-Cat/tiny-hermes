"""`runs` gains `purpose`.

`/compact` needs a model call — a summary is generated, and that costs money.
In this platform money is always owed by a Run: `record_summary_usage` takes a
non-null `run_id`, and §12.4's ceiling accumulates up `budget_root_run_id`.
A compaction with no Run has nothing to bill, so `/compact` creates one.

That reverses a decision this repository wrote down — a chat command never
becomes a Run (`Delivered`'s docstring: "`run` is `None` exactly when `receipt`
is not"). The reason that decision existed was that commands are pure data
operations that cost nothing. `/compact` is the first one that is not, so it is
the first exception, and the exception is narrow: this column says which kind a
Run is, and only `compaction` skips answering.

`answer` as the default is the only honest choice for existing rows: every Run
written before this migration was answering somebody, and a default of
`compaction` would make each of them claim it did work it never did.

The downgrade drops the column outright, like every other add-a-column
migration in this table's history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0054"
down_revision: str | None = "20260902_0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "purpose",
            sa.String(16),
            nullable=False,
            server_default="answer",
        ),
    )
    op.create_check_constraint(
        "ck_runs_purpose",
        "runs",
        "purpose IN ('answer', 'compaction')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_purpose", "runs", type_="check")
    op.drop_column("runs", "purpose")
