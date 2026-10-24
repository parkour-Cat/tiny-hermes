"""Which sandbox host each sandbox instance lives on (tier C, §21.3 v2.23).

Every row before this one was made by the only host there was, which is
`default`: the name a single host keeps (`SANDBOX_CONTROLLER_NAME`). The
index is for admission, which sums one host's live instances.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0078"
down_revision: str | None = "20261023_0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_instances",
        sa.Column(
            "controller", sa.String(64), nullable=False, server_default=sa.text("'default'")
        ),
    )
    op.create_index(
        "ix_sandbox_instances_controller_status", "sandbox_instances", ["controller", "status"]
    )


def downgrade() -> None:
    # Only a single host can go back: with two, which rows belonged where
    # would be lost, and each host would count the other's sandboxes again.
    hosts = op.get_bind().scalar(
        sa.text(
            "SELECT count(DISTINCT controller) FROM sandbox_instances "
            "WHERE controller <> 'default'"
        )
    )
    if hosts:
        raise RuntimeError(
            "sandbox instances live on hosts other than 'default'; retire them first"
        )
    op.drop_index("ix_sandbox_instances_controller_status", table_name="sandbox_instances")
    op.drop_column("sandbox_instances", "controller")
