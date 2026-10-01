"""`sandbox_instances.memory_mb`: the limit admission counts (§21.3, v2.14).

Backfill 1024: every instance before this revision was created with the one
M1 profile, whose limit was 1024 MiB and not configurable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0067"
down_revision: str | None = "20260929_0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_instances",
        sa.Column("memory_mb", sa.Integer(), nullable=False, server_default=sa.text("1024")),
    )


def downgrade() -> None:
    op.drop_column("sandbox_instances", "memory_mb")
