"""HTTP tools: a workspace's catalog of somebody else's API.

Two tables, the shape `skills` uses. The document body is a column rather than
an object in storage for the same reason a skill's files are: it is small text,
and one table makes immutability, the content hash and per-version rollback a
single foreign key instead of two stores to reconcile.

Nothing is seeded, and a registration is refused unless its host is already
inside the workspace's outbound scope — the tables added in 0016 are what that
check reads. A tool nobody may reach is not a tool worth registering.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0018"
down_revision: str | None = "20260818_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "http_tools",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=48), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_http_tools_workspace_name"),
    )
    op.create_index("ix_http_tools_workspace_id", "http_tools", ["workspace_id"])

    op.create_table(
        "http_tool_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "http_tool_id",
            sa.Uuid(),
            sa.ForeignKey("http_tools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("document_version", sa.String(length=64), nullable=False),
        # The platform's reading of the document, which is what a binding is
        # checked against and what the model is told about.
        sa.Column("operations", postgresql.JSONB(), nullable=False),
        # And the thing that was registered. An administrator comparing two
        # versions needs their own document, not this platform's reading of it.
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'withdrawn')", name="ck_http_tool_versions_status"
        ),
        # The same content is the same version: re-registering an unchanged
        # export is not a publication, and a version list that grew a row every
        # time somebody clicked twice would make rollback a guessing game.
        sa.UniqueConstraint(
            "http_tool_id", "content_hash", name="uq_http_tool_versions_content"
        ),
        sa.UniqueConstraint(
            "http_tool_id", "version_number", name="uq_http_tool_versions_number"
        ),
    )
    op.create_index(
        "ix_http_tool_versions_tool_id", "http_tool_versions", ["http_tool_id"]
    )
    op.create_foreign_key(
        "fk_http_tools_current_version",
        "http_tools",
        "http_tool_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint("fk_http_tools_current_version", "http_tools", type_="foreignkey")
    op.drop_index("ix_http_tool_versions_tool_id", table_name="http_tool_versions")
    op.drop_table("http_tool_versions")
    op.drop_index("ix_http_tools_workspace_id", table_name="http_tools")
    op.drop_table("http_tools")
