"""Indexes on two request paths: the API's session check reads a user's first
identity by `user_id`, and every Worker claim reads the unreleased leases.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0071"
down_revision: str | None = "20260930_0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_auth_identities_user", "auth_identities", ["user_id", "created_at"])
    op.create_index(
        "ix_worker_leases_live",
        "worker_leases",
        ["expires_at", "run_id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_worker_leases_live", table_name="worker_leases")
    op.drop_index("ix_auth_identities_user", table_name="auth_identities")
