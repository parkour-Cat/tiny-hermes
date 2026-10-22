"""The round a Run was in when it sent a call that may reach outside.

`inflight_round` is written in the transaction that charges such a call and
cleared when the round is recorded; recovery tells the model about it instead
of failing the Run (§17.2, v2.17). `interrupted_writes` holds the key each
interrupted HTTP write went out with, for an exact repeat to take again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261022_0072"
down_revision: str | None = "20261001_0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("inflight_round", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("interrupted_writes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "interrupted_writes")
    op.drop_column("runs", "inflight_round")
