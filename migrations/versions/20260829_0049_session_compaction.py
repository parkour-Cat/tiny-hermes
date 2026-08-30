"""压缩摘要的落脚点：每个 Session 一行，只留最新的一份。

只留最新一份是有意的，不是省事：§7.4.2 让后续压缩**更新**上一份摘要而不是
另起一行，所以"上一份"是唯一会被读的东西——`session_id` 上的唯一约束才是
这句话的执行者，`save_summary` 靠它 upsert。旧摘要不需要单独保留：它覆盖
的原文一条都没删，真要追溯覆盖了什么，走 `CONTEXT_COMPACTED` 这条 RunEvent，
不走这张表的历史。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0049"
down_revision: str | None = "20260826_0048"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "session_compactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("first_sequence", sa.Integer(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_session_compactions_session",
            ondelete="CASCADE",
        ),
        # `session_id` gets no separate index: this constraint's own unique
        # index already serves every lookup by `session_id`, and a second one
        # over the same single column would just be upkeep with nothing to
        # answer that the first doesn't.
        sa.UniqueConstraint(
            "session_id", name="uq_session_compactions_session"
        ),
        sa.CheckConstraint(
            "source IN ('model', 'structural')",
            name="ck_session_compactions_source",
        ),
    )


def downgrade() -> None:
    op.drop_table("session_compactions")
