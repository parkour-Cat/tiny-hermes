"""Expire model discovery approvals without retaining keys or endpoints."""

import sqlalchemy as sa
from alembic import op

revision = "20260907_0059"
down_revision = "20260907_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_discovery_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_model_discovery_grants_expires_at", "model_discovery_grants", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("model_discovery_grants")
