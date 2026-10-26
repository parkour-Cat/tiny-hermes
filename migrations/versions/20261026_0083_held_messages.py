"""A Feishu message waits on its own delivery row until it is gathered.

Arriving no longer starts a Run (office-tasks design, 2026-10-26): Feishu
sends a file and the words typed beside it as two messages in either order,
so the Scheduler gathers what one person sent within a short window into one
Run. Until then the message — words, images, files — is held on the row
that claimed it, which is the one place that exists for a first-ever
message too, the way `command_receipt` and `unsupported_kind` live there.

`held_message` is the message; `held_open_id` is whose batch it belongs to;
`held_taken_at` is when a gather took it, or `/new` dropped it. A message
that is only files and has nobody's words to go with is asked about by the
reply scan, settled through `replied_at`/`reply_note` like every other
answer here.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261026_0083"
down_revision: str | None = "20261025_0082"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("held_message", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("held_open_id", sa.String(120), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("held_taken_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The gather's lookup, and `/new`'s: what this person still has waiting.
    op.create_index(
        "ix_channel_events_held_by_sender",
        "channel_events",
        ["channel_binding_id", "held_open_id"],
        postgresql_where=sa.text("held_message IS NOT NULL AND held_taken_at IS NULL"),
    )
    # The question scan's.
    op.create_index(
        "ix_channel_events_awaiting_held_question",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text("held_message IS NOT NULL AND replied_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_events_awaiting_held_question", table_name="channel_events"
    )
    op.drop_index("ix_channel_events_held_by_sender", table_name="channel_events")
    op.drop_column("channel_events", "held_taken_at")
    op.drop_column("channel_events", "held_open_id")
    op.drop_column("channel_events", "held_message")
