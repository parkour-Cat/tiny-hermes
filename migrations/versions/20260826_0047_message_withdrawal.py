"""一条消息可以被它的作者收回，而不被删掉。

软隐藏而非删除，因为 `runs/domain/context_budget.py` 写下的不变量是
「No branch makes a message unreachable」。撤回把消息挡在模型上下文之外，
不把它挡在转写记录和审计之外。

与 `redacted` 是两件事，所以是两列：`redacted` 是 §344 的擦除（等于不存在），
撤回是「用户收回了」——转写记录仍然要显示它，标为已撤回。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0047"
down_revision: str | None = "20260825_0046"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "session_messages",
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_messages", "withdrawn_at")
