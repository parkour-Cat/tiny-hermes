"""A Completions Run remembers it must hold its Worker lease.

Runs created through the asynchronous API leave this column null. Chat
Completions sets `chat_completions` so the Worker can skip ordinary slice
re-queueing inside the Agent's sync window.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0010"
down_revision: str | None = "20260813_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("delivery_mode", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_runs_delivery_mode",
        "runs",
        "delivery_mode IS NULL OR delivery_mode IN ('chat_completions')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_delivery_mode", "runs", type_="check")
    op.drop_column("runs", "delivery_mode")
