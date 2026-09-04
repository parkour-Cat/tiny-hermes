"""`channel_bindings` gains `long_connection_seen_at`.

`transport` says what a binding is *configured* to use, and nothing else.
A long connection that died an hour ago still reads `long_connection`,
which is exactly what the console shows — its own column comment admits
it: "the response carries the stored transport and nothing about the
running scheduler".

On 2026-09-03 a binding's socket died at 23:53 and nothing reconnected for
ten hours. The console said 长连接 the whole time. The person running it
found out by sending a message that never arrived.

This column is the one thing that can tell "configured as a long
connection" apart from "connected right now". The scheduler's liveness
loop writes it while the socket is up; a reader judges by **how old it
is**, not by whether it exists — a scheduler that was killed leaves a
timestamp that stops advancing, not a `NULL`.

Nullable with no default and no backfill: every existing binding has never
been confirmed alive by this mechanism, and inventing a timestamp would
make each of them claim a liveness nobody observed. `NULL` reads as "never
seen", which is the truth until the next heartbeat.

The downgrade drops the column outright, like every other add-a-column
migration in this table's history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0056"
down_revision: str | None = "20260903_0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_bindings",
        sa.Column("long_connection_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_bindings", "long_connection_seen_at")
