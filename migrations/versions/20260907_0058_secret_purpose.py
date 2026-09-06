"""Classify credentials explicitly; existing entries remain unclassified."""

import sqlalchemy as sa
from alembic import op

revision = "20260907_0058"
down_revision = "20260906_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "secrets", sa.Column("purpose", sa.String(32), nullable=False, server_default="general")
    )
    op.create_check_constraint(
        "ck_secrets_purpose",
        "secrets",
        "purpose IN ('general', 'model', 'tool', 'channel', 'login')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_secrets_purpose", "secrets", type_="check")
    op.drop_column("secrets", "purpose")
