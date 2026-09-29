"""`runs.queued_at`: claim Runs by when they last entered the queue (§21.3, v2.13).

Backfill: a Run queued now has most recently changed by entering the queue or
by something that left it there, so `updated_at` is the closest record of when
it started waiting. Every other Run gets `created_at`; it is rewritten the next
time the Run enters the queue.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0066"
down_revision: str | None = "20260927_0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE runs SET queued_at = CASE WHEN status = 'queued' THEN updated_at "
        "ELSE created_at END"
    )
    op.alter_column("runs", "queued_at", nullable=False, server_default=sa.text("now()"))
    op.create_index(
        "ix_runs_claim_order",
        "runs",
        ["queued_at", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_claim_order", table_name="runs")
    op.drop_column("runs", "queued_at")
