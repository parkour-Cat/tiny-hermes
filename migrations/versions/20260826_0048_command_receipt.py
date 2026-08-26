"""一条命令做成了什么，也是欠发信人的一句话。

`/undo`、`/new` 不产生 Run（Task 4 的 `RunCoordination.withdraw_from_session`
直接在入站事务里完成撤回），所以 `pending_replies` 那条 join 到 `runs` 的
扫描永远看不见它们——沿用它就是继续沉默，而 §19.2 已经因为一张阻塞卡片和
一条不认得的消息类型两次禁止过这件事。

新开一对列，而不是复用 `unsupported_kind` / `unsupported_open_id`：「这个
消息类型不支持」和「你的命令执行了」是要跟人说的两句不同的话，共用一列
会让扫描器分不清自己欠的是哪一句。`command_receipt` 存 `CommandReceipt`
的文档（做法同 `blocked_notice`——入站那一刻是唯一准确的时刻，渲染留给
飞书层）；`command_open_id` 是发信人，做法同 `unsupported_open_id`——命令
本身不落 `channel_conversations`，扫描器要靠这一列才找得到人。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0048"
down_revision: str | None = "20260826_0047"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_events",
        sa.Column("command_receipt", sa.JSON(), nullable=True),
    )
    op.add_column(
        "channel_events",
        sa.Column("command_open_id", sa.String(120), nullable=True),
    )
    op.create_index(
        "ix_channel_events_awaiting_command_receipt",
        "channel_events",
        ["received_at"],
        postgresql_where=sa.text(
            "command_receipt IS NOT NULL AND replied_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_events_awaiting_command_receipt", table_name="channel_events"
    )
    op.drop_column("channel_events", "command_open_id")
    op.drop_column("channel_events", "command_receipt")
