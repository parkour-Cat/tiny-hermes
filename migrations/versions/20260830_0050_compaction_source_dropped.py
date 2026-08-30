"""`session_compactions.source` 没有读者，删掉它。

0049 建这一列时给的理由是「判断这段文字该信到什么程度的读者需要分得出模型摘要
和结构摘要」。那个读者从来没写出来，也不该写在这张表上：结构摘要根本不落库——
它每轮由 `plan_context` 免费重算——所以这张表里的每一行都是模型写的，这一列只
有一个可能的取值。**一条注释不得声称代码没有的保护。**

运维真正需要的那个区分在 `CONTEXT_COMPACTED` 事件上，由 `CompactionRecord`
写、运行台按它分句。那条路是有读者的。

单独一次迁移而不是改回 0049：0049 已经在跑着的环境里执行过了，alembic 不会因为
文件变了就重跑它。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0050"
down_revision: str | None = "20260829_0049"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.drop_constraint("ck_session_compactions_source", "session_compactions")
    op.drop_column("session_compactions", "source")
    # 顺手补上 0049 漏掉的这条索引。ORM 一直写着 `index=True`，迁移里没有它，
    # 于是 `uv run alembic check` 在 0049 上就是红的——CI 跑的正是这条命令。
    # 每张带 workspace_id 的表都有这条索引（`ix_runs_workspace_id` 等），这里
    # 不是例外，只是被忘了。
    op.create_index(
        "ix_session_compactions_workspace_id",
        "session_compactions",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_session_compactions_workspace_id", table_name="session_compactions")
    # `'model'` 作为回填值不是猜的：结构摘要不落库，这张表历史上写下的每一行都
    # 是模型写的。server_default 随即撤掉，好让这一列回到 0049 的样子——那时候
    # 它是必填、且由写入方给值的。
    op.add_column(
        "session_compactions",
        sa.Column("source", sa.String(16), nullable=False, server_default="model"),
    )
    op.alter_column("session_compactions", "source", server_default=None)
    op.create_check_constraint(
        "ck_session_compactions_source",
        "session_compactions",
        "source IN ('model', 'structural')",
    )
