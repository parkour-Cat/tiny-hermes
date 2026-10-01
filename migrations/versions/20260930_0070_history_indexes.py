"""Indexes for queries that read growing history: expired idempotency records,
run events past retention, and a Run's own session messages.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0070"
down_revision: str | None = "20260930_0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_idempotency_records_expiry",
        "idempotency_records",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index("ix_run_events_occurred_at", "run_events", ["occurred_at"])
    op.create_index(
        "ix_session_messages_source_run",
        "session_messages",
        ["source_run_id", "sequence"],
        postgresql_where=sa.text("source_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_session_messages_source_run", table_name="session_messages")
    op.drop_index("ix_run_events_occurred_at", table_name="run_events")
    op.drop_index("ix_idempotency_records_expiry", table_name="idempotency_records")
