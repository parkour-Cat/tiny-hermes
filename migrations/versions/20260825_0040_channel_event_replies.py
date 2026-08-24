"""`channel_events` becomes the outbound queue as well as the inbound claim.

Three columns, and one reason for all of them: a Run finishing has to be
noticed **exactly once** by something that can crash, and a reply that goes
out twice is worse than one that goes out late.

`replied_at` is the stamp that makes the scan drain. Without it the
dispatcher re-sends the same answer every interval for as long as the row
survives the seven-day sweep.

`reply_attempts` bounds the retries. A refusal like "bot is not in the
chat" never becomes true by trying again, and a row retried forever is a
scan that never finishes.

`reply_note` is what an operator reads. This project shipped `audit_events`
append-only with nobody reading it and wrote that down as a debt; the note
is the opposite choice, made deliberately — a settled row says *how* it
settled (`sent`, `binding_disabled`, `no_credential`, `refused:…`), so
"the reply never arrived" is answerable from one row rather than from logs
that have rotated.

The queue reuses this table rather than getting its own, because the claim
and the reply are the same delivery. A second table would need its own
insert on the inbound path — one more write to forget, and forgetting it
would be silent.

The partial index matches the scan's predicate exactly. `channel_events`
keeps seven days of every delivery and the unreplied set is a handful of
rows; a full scan of the table to find them would grow with traffic that
has nothing to do with the work.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0040"
down_revision: str | None = "20260824_0039"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column(
            "reply_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "channel_events", sa.Column("reply_note", sa.String(200), nullable=True)
    )
    op.create_check_constraint(
        "ck_channel_events_reply_attempts",
        "channel_events",
        "reply_attempts >= 0",
    )
    op.create_index(
        "ix_channel_events_awaiting_reply",
        "channel_events",
        ["run_id"],
        postgresql_where=sa.text("run_id IS NOT NULL AND replied_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_channel_events_awaiting_reply", table_name="channel_events")
    op.drop_constraint("ck_channel_events_reply_attempts", "channel_events")
    op.drop_column("channel_events", "reply_note")
    op.drop_column("channel_events", "reply_attempts")
    op.drop_column("channel_events", "replied_at")
