"""A Feishu message that is only a file waits on its own delivery row.

It starts no Run (office-tasks design, "只有文件的消息"), so there is no Run
to hang the file on until the sender's next message — and no
`channel_conversations` row either for a first-ever message that is a file.
The delivery row is the one place that exists for it, so the hold lives
there, the way `command_receipt` and `unsupported_kind` do.

Three columns rather than one: `held_files` is what to attach, `held_open_id`
is whose next message takes it, and `held_taken_at` is the difference
between "still waiting" and "gone with a message" — which the scan reads to
decide whether the sender still needs asking. `replied_at`/`reply_note`
settle the question itself, as they settle every other answer here.
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
        sa.Column("held_files", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("held_open_id", sa.String(120), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("held_taken_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The next message's lookup: what this person still has waiting.
    op.create_index(
        "ix_channel_events_held_by_sender",
        "channel_events",
        ["channel_binding_id", "held_open_id"],
        postgresql_where=sa.text("held_files IS NOT NULL AND held_taken_at IS NULL"),
    )
    # The scan's, matching its predicate exactly.
    op.create_index(
        "ix_channel_events_awaiting_held_question",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text("held_files IS NOT NULL AND replied_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_events_awaiting_held_question", table_name="channel_events"
    )
    op.drop_index("ix_channel_events_held_by_sender", table_name="channel_events")
    op.drop_column("channel_events", "held_taken_at")
    op.drop_column("channel_events", "held_open_id")
    op.drop_column("channel_events", "held_files")
