"""A slow Run tells the person it is still working, once.

§19.2 asks for `流式能力受渠道限制时的进度更新`. Feishu has no streaming,
so a Run that takes two minutes shows the sender nothing — and this module
has now produced the same failure four times over: silence looks exactly
like a message that was dropped, and the person sends it again.

Its own stamp, like `blocked_notified_at` and for the same reason: one
delivery can now produce a progress note *and* an answer, and a shared
stamp would settle the row before the answer existed.

**Once, deliberately.** A stamp rather than a counter is the design: there
is no second notice to schedule, so there is no interval to tune and no way
for a ten-minute Run to produce thirty messages. Step-by-step progress is
not in this build at all — what a Run is doing is tool names and internal
state, which §19.1 keeps off an end-user surface.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0043"
down_revision: str | None = "20260825_0042"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("progress_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_channel_events_awaiting_progress",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text(
            "run_id IS NOT NULL AND replied_at IS NULL"
            " AND progress_notified_at IS NULL AND blocked_notice IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_channel_events_awaiting_progress", table_name="channel_events")
    op.drop_column("channel_events", "progress_notified_at")
