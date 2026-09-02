"""`sessions` gains `compaction_requested_at`.

`/compact` lets a person compact their own conversation instead of waiting for
`compaction_threshold` to be crossed. It cannot do the compaction where the
command arrives: a summary is a real model call, and the inbound path must not
depend on a model endpoint being reachable — the same reason a blocked notice
is *recorded* on delivery and *sent* by the outbound scan. A model timeout there
would surface as a delivery failure and six hours of Feishu retries.

So the command writes this column, and the next round consumes it before it
plans anything. Nullable rather than a boolean with a default: `NULL` means "not
asked for", and a timestamp says *when* — which is what tells a later reader
whether a request that never got consumed is minutes old or from last month.

The column is cleared on consumption, so a request never fires twice. It is
cleared even when there was nothing to compact: leaving it set would make a
short conversation carry a pending request that surprises somebody weeks later,
long after the receipt that explained it scrolled away.

The downgrade drops the column outright, like every other add-a-column migration
in this table's history: nothing else references it, so there is no narrower row
set to clean up first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0053"
down_revision: str | None = "20260831_0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "compaction_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "compaction_requested_at")
