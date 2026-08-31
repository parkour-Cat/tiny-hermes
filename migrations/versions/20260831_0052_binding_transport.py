"""`channel_bindings` gains `transport`.

§19.2 requires both a Webhook and a WebSocket long connection as inbound
transports, and a binding has to say which one it uses. The design doc
(§19.2) recommends `long_connection` as the default for a *new*, private
deployment — but that recommendation is about what a fresh install should
pick, not about what this column's default should be. This column's default
decides what an **existing** row gets the instant this migration runs, and
every existing binding is receiving messages through a public webhook
address right now. Defaulting the column to `long_connection` would not
change their behavior — nothing repoints their delivery — it would just
make their `transport` value lie about how they actually receive events,
until whatever reads this column starts believing the lie and the binding
goes deaf with no error anywhere. `webhook` is the only default that keeps
today's read matching today's reality.

The downgrade drops the column outright, the same as every other
add-a-column migration in this table's history (0037, 0039): there is no
narrower row set to delete first, unlike the event-type widenings.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0052"
down_revision: str | None = "20260830_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_bindings",
        sa.Column(
            "transport",
            sa.String(32),
            nullable=False,
            server_default="webhook",
        ),
    )
    op.create_check_constraint(
        "ck_channel_bindings_transport",
        "channel_bindings",
        "transport IN ('webhook', 'long_connection')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_channel_bindings_transport", "channel_bindings", type_="check")
    op.drop_column("channel_bindings", "transport")
