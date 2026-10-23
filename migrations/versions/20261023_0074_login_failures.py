"""Failed password logins, by account, for throttling (§23.16, v2.19).

`subject_key` is the SHA-256 of the normalised account name, never the name:
people type passwords into the account field. Rows older than the throttle
window decide nothing and the Scheduler deletes them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0074"
down_revision: str | None = "20261023_0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_failures",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("subject_key", sa.String(64), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_login_failures_subject_failed_at", "login_failures", ["subject_key", "failed_at"]
    )
    op.create_index("ix_login_failures_failed_at", "login_failures", ["failed_at"])


def downgrade() -> None:
    op.drop_index("ix_login_failures_failed_at", table_name="login_failures")
    op.drop_index("ix_login_failures_subject_failed_at", table_name="login_failures")
    op.drop_table("login_failures")
