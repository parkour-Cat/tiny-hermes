"""Approved outbound targets, at the platform level and the workspace level.

One table for both, the shape `skills` uses: a `level` column with
`workspace_id` null exactly when it says `platform`. Two tables would have been
two of every query, and two chances for one level to gain a rule the other did
not.

Nothing is seeded. An empty table means the platform approves nothing, which is
the same answer an empty `SANDBOX_IMAGE_DIGEST` gives the sandbox: a deployment
that has configured no outbound scope sends nothing rather than everything.
Product design §16.5 puts the widening in a platform administrator's hands, so
it starts closed and somebody opens it deliberately.

`endpoint_id` is not a foreign key. A registered model endpoint's host is
approved by registering it, and the row is removed when the endpoint is
disabled — but an endpoint deleted out from under this table must not take a
platform administrator's own entry with it, and the two live in different
modules with no dependency between them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0016"
down_revision: str | None = "20260817_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_scopes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("entry", sa.String(length=255), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("endpoint_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "level IN ('platform', 'workspace')", name="ck_outbound_scopes_level"
        ),
        sa.CheckConstraint(
            "(level = 'platform' AND workspace_id IS NULL) OR "
            "(level = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_outbound_scopes_level_workspace",
        ),
    )
    op.create_index(
        "ix_outbound_scopes_workspace_id", "outbound_scopes", ["workspace_id"]
    )
    op.create_index("ix_outbound_scopes_endpoint_id", "outbound_scopes", ["endpoint_id"])
    # Partial, because the two levels name independently: a workspace approving
    # `api.example.com` is a different row from the platform approving it, and
    # the workspace's is what survives the intersection either way.
    op.create_index(
        "uq_outbound_scopes_workspace_entry",
        "outbound_scopes",
        ["workspace_id", "entry"],
        unique=True,
        postgresql_where=sa.text("level = 'workspace'"),
    )
    op.create_index(
        "uq_outbound_scopes_platform_entry",
        "outbound_scopes",
        ["entry"],
        unique=True,
        postgresql_where=sa.text("level = 'platform'"),
    )


def downgrade() -> None:
    op.drop_index("uq_outbound_scopes_platform_entry", table_name="outbound_scopes")
    op.drop_index("uq_outbound_scopes_workspace_entry", table_name="outbound_scopes")
    op.drop_index("ix_outbound_scopes_endpoint_id", table_name="outbound_scopes")
    op.drop_index("ix_outbound_scopes_workspace_id", table_name="outbound_scopes")
    op.drop_table("outbound_scopes")
