"""Sandbox hosts, each registered by its own controller (tier C §4.1).

Empty until a controller of this version starts; the single host of every
existing deployment then registers itself as `default`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0079"
down_revision: str | None = "20261023_0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_hosts",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("url", sa.String(255), nullable=True),
        sa.Column("memory_budget_mb", sa.Integer(), nullable=True),
        sa.Column("network_pool", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'draining', 'retired')", name="ck_sandbox_hosts_state"
        ),
    )


def downgrade() -> None:
    op.drop_table("sandbox_hosts")
