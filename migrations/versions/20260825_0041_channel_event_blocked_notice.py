"""A queued delivery remembers that it was queued, so it can say so.

§19.2 requires a status card when a Session's head Run is blocked, and
§497 lists what it must contain. All of it was already computed on the
inbound path — and written into the webhook's HTTP response body, which
Feishu's server discards on reading the 200. Nobody ever saw it.

Sending it needs it to survive the request, for the same reason the reply
does: sending inside the webhook transaction would make an inbound delivery
depend on `open.feishu.cn` being reachable, and a timeout there would make
Feishu retry a delivery whose claim was already taken — the message lost
with nothing left to say so.

**Stored, not re-derived.** The inbound moment is the only accurate one.
The head can unblock two seconds later, but "your message landed second in
the queue" was true when it landed, and that is the sentence the person
needs. A scan that re-read the queue would describe a world that had
already moved on, and would say nothing at all in the common case where it
moved on quickly.

`blocked_notified_at` is separate from `replied_at` rather than sharing it.
One delivery now produces two sends — the notice now, the answer when the
Run finally runs — and a single stamp would settle the row before the
answer existed, which is exactly the silence this migration exists to end.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260825_0041"
down_revision: str | None = "20260825_0040"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("blocked_notice", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("blocked_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_channel_events_awaiting_notice",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text(
            "blocked_notice IS NOT NULL AND blocked_notified_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_channel_events_awaiting_notice", table_name="channel_events")
    op.drop_column("channel_events", "blocked_notified_at")
    op.drop_column("channel_events", "blocked_notice")
