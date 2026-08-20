"""Approvals: the record of a person deciding, and who that person may be.

Product design §16.3, and three shape decisions worth reading.

**The normalized call is stored twice.** `content_hash` is what the platform
compares on the next round and `document` is what a person is shown. The hash
alone would mean a reviewer approving a string of hex; the document alone would
mean recomputing the hash on every read, and a normalization that changed would
silently revalidate approvals nobody granted again.

**One pending approval per Run, as a partial unique index.** A Run is stopped
while it waits, so a second pending row could only come from a duplicated
request — and two rows a person could answer differently is a state nothing
downstream knows how to read.

**`runs.end_user_id` is nullable, and that is the rule rather than a gap.** A
`user_confirmation` may only be answered by the EndUser who started the Run, so
a ServiceAccount's Run — which has none — cannot use one at all. §16.3 requires
exactly that: such an Agent must have chosen a pre-authorization or a
governance approval at publish, or it does not publish.

`workspaces.approval_validity_seconds` carries §16.3's configurable window,
constrained to its five-minute floor and seven-day ceiling here as well as
clamped in the domain. The constraint stops an impossible value from being
stored; the clamp stops one that somehow was from failing somebody's Run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0020"
down_revision: str | None = "20260818_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("approval_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("required_permission", sa.String(length=128), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.String(length=2048), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL AND decided_by IS NULL)",
            name="ck_approvals_decision_complete",
        ),
        sa.CheckConstraint(
            "approval_type IN ('user_confirmation', 'governance_approval')",
            name="ck_approvals_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')", name="ck_approvals_status"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name="fk_approvals_requested_by", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"], ["runs.id", "runs.workspace_id"], name="fk_approvals_run"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approvals_content_hash"), "approvals", ["content_hash"], unique=False)
    op.create_index(
        "ix_approvals_expiry",
        "approvals",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(op.f("ix_approvals_run_id"), "approvals", ["run_id"], unique=False)
    op.create_index(op.f("ix_approvals_workspace_id"), "approvals", ["workspace_id"], unique=False)
    op.create_index(
        "ix_approvals_workspace_status", "approvals", ["workspace_id", "status"], unique=False
    )
    op.create_index(
        "uq_approvals_pending_run",
        "approvals",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.add_column("runs", sa.Column("end_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_runs_end_user", "runs", "users", ["end_user_id"], ["id"], ondelete="RESTRICT"
    )
    op.add_column("workspaces", sa.Column("approval_validity_seconds", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_approval_validity",
        "workspaces",
        "approval_validity_seconds IS NULL OR approval_validity_seconds BETWEEN 300 AND 604800",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_approval_validity", "workspaces", type_="check")
    op.drop_column("workspaces", "approval_validity_seconds")
    op.drop_constraint("fk_runs_end_user", "runs", type_="foreignkey")
    op.drop_column("runs", "end_user_id")
    op.drop_index(
        "uq_approvals_pending_run",
        table_name="approvals",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_approvals_workspace_status", table_name="approvals")
    op.drop_index(op.f("ix_approvals_workspace_id"), table_name="approvals")
    op.drop_index(op.f("ix_approvals_run_id"), table_name="approvals")
    op.drop_index(
        "ix_approvals_expiry",
        table_name="approvals",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(op.f("ix_approvals_content_hash"), table_name="approvals")
    op.drop_table("approvals")
