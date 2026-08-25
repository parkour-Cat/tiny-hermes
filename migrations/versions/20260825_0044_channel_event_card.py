"""One card per delivery, so it can be rewritten instead of added to.

Feishu has no typing indicator for bots. What it has is
`PATCH /im/v1/messages/{message_id}`, which rewrites a card already in the
conversation — and that is what makes an immediate response possible at
all. A text message cannot be taken back, so the platform had to know what
to say before saying anything: 8 seconds for a progress note, longer for
the answer. Silence is indistinguishable from a dropped message.

`card_message_id` is Feishu's own id for the card this delivery opened
with. Nullable, and the null case is load-bearing rather than an edge: an
answer that carried no id, or a send that failed outright, leaves nothing
to patch — and the reply still has to arrive, as a new message. A design
that could only speak through a card would go silent exactly when the card
was unreachable.

`card_attempted_at` is separate from `card_message_id` for the null case:
a send that answered without an id, or failed outright, must leave the
opening scan even though there is no id to store. Without it that delivery
would sit in the scan forever, sending a new 「正在处理」 card every second.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0044"
down_revision: str | None = "20260825_0043"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("card_message_id", sa.String(120), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("card_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_channel_events_awaiting_card",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text(
            "run_id IS NOT NULL AND card_message_id IS NULL"
            " AND replied_at IS NULL AND card_attempted_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_channel_events_awaiting_card", table_name="channel_events")
    op.drop_column("channel_events", "card_attempted_at")
    op.drop_column("channel_events", "card_message_id")
