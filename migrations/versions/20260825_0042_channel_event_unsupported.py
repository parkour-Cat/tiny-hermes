"""A delivery this build cannot read still owes its sender an answer.

§19.2 forbids swallowing a message quietly, and the blocked-head path was
only half of it. A photo, a voice note or a file produced a 200 and a log
line — and a log line is not read by the person who sent it. The comment on
that branch claimed `never silently`; it was true about the platform's
records and false about the only reader who mattered.

`unsupported_open_id` is who to send it to, kept on the delivery rather
than looked up. A first message that happens to be a photo has no
`channel_conversations` row — nobody has ever started a session — and that
is precisely the person most in need of being told why nothing happened.

`unsupported_kind` is what the refusal says. Nullable, because the ordinary
delivery is readable — and there is deliberately no CHECK tying it to
`run_id` being NULL: an unreadable message starts no Run, but writing that
as a constraint would forbid a future build that could read a photo *and*
wanted to record the type it had been sent.

No new stamp. `replied_at` and `reply_note` already mean "this delivery has
been answered", and a refusal is an answer. The scan finds these rows
without touching `runs` at all, which is what makes it a separate branch
rather than a special case inside the reply scan.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0042"
down_revision: str | None = "20260825_0041"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("unsupported_kind", sa.String(64), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("unsupported_open_id", sa.String(200), nullable=True),
    )
    op.create_index(
        "ix_channel_events_awaiting_refusal",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text(
            "unsupported_kind IS NOT NULL AND replied_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_channel_events_awaiting_refusal", table_name="channel_events")
    op.drop_column("channel_events", "unsupported_open_id")
    op.drop_column("channel_events", "unsupported_kind")
