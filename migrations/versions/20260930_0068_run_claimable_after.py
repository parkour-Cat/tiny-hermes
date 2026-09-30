"""`runs.claimable_after`: a Run refused sandbox room waits before it is claimed (§21.3).

Nullable, no backfill: no Run is waiting before this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0068"
down_revision: str | None = "20260930_0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("claimable_after", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "claimable_after")
