"""Per-workspace running limits (product design §1.21.3, v2.21).

`max_running_runs_per_caller`: how many Runs one person (a user or an end
user) may have executing at once in this workspace; null takes the platform's
`RUN_LIMIT_PER_CALLER`. `max_running_runs`: the whole workspace's; null is no
limit. Runs over either wait in the queue, they are not refused.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0076"
down_revision: str | None = "20261023_0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces", sa.Column("max_running_runs_per_caller", sa.Integer(), nullable=True)
    )
    op.add_column("workspaces", sa.Column("max_running_runs", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_running_limits_positive",
        "workspaces",
        "(max_running_runs_per_caller IS NULL OR max_running_runs_per_caller > 0) "
        "AND (max_running_runs IS NULL OR max_running_runs > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_running_limits_positive", "workspaces", type_="check")
    op.drop_column("workspaces", "max_running_runs")
    op.drop_column("workspaces", "max_running_runs_per_caller")
