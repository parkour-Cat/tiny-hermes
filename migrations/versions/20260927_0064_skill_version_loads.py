"""`skill_version_loads`: which Runs loaded which skill version (§15.4, v2.12).

The evidence a reviewer reads. A table of its own because the `skill_loaded`
events it mirrors are pruned with their terminal Run's events after the
retention window. Backfilled from the events still present; a Run whose
events are already gone is not recoverable, and the counts start from there.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0064"
down_revision: str | None = "20260927_0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_version_loads",
        sa.Column(
            "skill_version_id",
            sa.Uuid(),
            sa.ForeignKey("skill_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_version_id", "run_id", name="uq_skill_version_loads_run"),
    )
    op.create_index(
        "ix_skill_version_loads_workspace_version",
        "skill_version_loads",
        ["workspace_id", "skill_version_id"],
    )
    op.execute(
        """
        INSERT INTO skill_version_loads (skill_version_id, run_id, workspace_id, loaded_at)
        SELECT DISTINCT ON (v.id, e.run_id) v.id, e.run_id, e.workspace_id, e.occurred_at
        FROM run_events e
        JOIN skill_versions v ON v.id::text = e.payload->>'skill_version_id'
        WHERE e.event_type = 'skill_loaded'
        ORDER BY v.id, e.run_id, e.occurred_at
        """
    )


def downgrade() -> None:
    op.drop_index("ix_skill_version_loads_workspace_version", table_name="skill_version_loads")
    op.drop_table("skill_version_loads")
