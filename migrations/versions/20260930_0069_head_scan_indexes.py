"""Partial indexes for the Scheduler's head scan (§11.1): live Runs by Session,
and Sessions that have a head. Both grow with live work, not with history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0069"
down_revision: str | None = "20260930_0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_runs_live_by_session",
        "runs",
        ["session_id", "session_sequence"],
        postgresql_where=sa.text("status NOT IN ('completed', 'failed', 'cancelled')"),
    )
    op.create_index(
        "ix_sessions_with_head",
        "sessions",
        ["id"],
        postgresql_where=sa.text("head_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_with_head", table_name="sessions")
    op.drop_index("ix_runs_live_by_session", table_name="runs")
