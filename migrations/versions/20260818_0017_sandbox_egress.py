"""Which Run a sandbox address belongs to, for the moment it holds one.

The proxy identifies a sandbox by the address its packets came from, because a
process inside a container that holds a credential is a process that can lend
one. That only works if something writes the mapping down, and the only thing
that knows a container's address is the Controller that created it.

Rows are short-lived on purpose. A frozen instance may not open a new
connection (§16.4), and a destroyed one must not hand its identity to whatever
Docker gives the address to next — so the row is removed on freeze and on
destroy, and written again on thaw.

`address` is the primary key rather than the sandbox: two containers cannot
share an address at one time, and that is exactly the uniqueness the proxy
depends on when it answers "who is this".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0017"
down_revision: str | None = "20260818_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_egress_addresses",
        sa.Column("address", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_sandbox_egress_addresses_sandbox_id",
        "sandbox_egress_addresses",
        ["sandbox_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sandbox_egress_addresses_sandbox_id", table_name="sandbox_egress_addresses"
    )
    op.drop_table("sandbox_egress_addresses")
