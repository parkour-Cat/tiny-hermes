"""Published sites (§1.26, v2.26).

A site is a pinned workspace revision served from its own origin. The link's
token is stored only as its digest; a publication goes with the revision it
pins, and so with the session that revision belongs to.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261025_0081"
down_revision: str | None = "20261024_0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_publications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("root", sa.String(512), nullable=False),
        sa.Column("title", sa.String(100), nullable=True),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "visibility IN ('members', 'link')", name="ck_site_publications_visibility"
        ),
        sa.CheckConstraint("length(token_sha256) = 64", name="ck_site_publications_token"),
        sa.ForeignKeyConstraint(
            ["revision_id", "workspace_id"],
            ["workspace_revisions.id", "workspace_revisions.workspace_id"],
            name="fk_site_publications_revision",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_site_publications_workspace_id", "site_publications", ["workspace_id"])
    op.create_index("ix_site_publications_revision_id", "site_publications", ["revision_id"])
    op.create_table(
        "site_ticket_uses",
        sa.Column("nonce", sa.String(64), primary_key=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("site_ticket_uses")
    op.drop_index("ix_site_publications_revision_id", table_name="site_publications")
    op.drop_index("ix_site_publications_workspace_id", table_name="site_publications")
    op.drop_table("site_publications")
