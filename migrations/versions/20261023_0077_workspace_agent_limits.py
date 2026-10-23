"""Per-workspace ceilings on what an Agent may ask for (§1.21.1, v2.21).

Null takes the platform's `AGENT_MAX_EXECUTION_SECONDS` and
`AGENT_MAX_MODEL_CALLS`, which is what every workspace had before; the API
never stores more than the platform's hard ceilings.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0077"
down_revision: str | None = "20261023_0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces", sa.Column("agent_max_execution_seconds", sa.Integer(), nullable=True)
    )
    op.add_column("workspaces", sa.Column("agent_max_model_calls", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_agent_limits_positive",
        "workspaces",
        "(agent_max_execution_seconds IS NULL OR agent_max_execution_seconds > 0) "
        "AND (agent_max_model_calls IS NULL OR agent_max_model_calls > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_agent_limits_positive", "workspaces", type_="check")
    op.drop_column("workspaces", "agent_max_model_calls")
    op.drop_column("workspaces", "agent_max_execution_seconds")
