"""A Run may read a file it did not produce, and only because a row says so.

Product design §13, eighth clause: files move between a parent and a child as
**authorizations**, never through a shared directory. This is the table those
authorizations live in, and two things about its shape are the point rather
than incidental.

**A grant belongs to a Run, not to an Agent.** The question a read asks is "may
*this* Run see this file". A grant keyed by Agent would let a later, unrelated
Run of the same Agent read something nobody passed it — and the whole reason
files move this way is that reachability is decided per piece of work.

**One row per pair.** Granting twice is ordinary: a parent may hand the same
file to two children, and a redelivered result re-grants. With a unique pair
"is this granted" stays a membership question rather than a counting one.

`reason` says which way it went. It is never consulted when a read is checked —
a grant is a grant — but without it "why can this Run read this" stops being
answerable, and a bare pair of ids cannot answer it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0029"
down_revision: str | None = "20260819_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_grants",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reason IN ('delegated_down', 'delivered_up')",
            name="ck_artifact_grants_reason",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name="fk_artifact_grants_artifact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_artifact_grants_run",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_artifact_grants_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "run_id", name="uq_artifact_grants_pair"),
    )
    op.create_index("ix_artifact_grants_workspace_id", "artifact_grants", ["workspace_id"])
    op.create_index("ix_artifact_grants_artifact_id", "artifact_grants", ["artifact_id"])
    op.create_index("ix_artifact_grants_run_id", "artifact_grants", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_grants_run_id", table_name="artifact_grants")
    op.drop_index("ix_artifact_grants_artifact_id", table_name="artifact_grants")
    op.drop_index("ix_artifact_grants_workspace_id", table_name="artifact_grants")
    op.drop_table("artifact_grants")
