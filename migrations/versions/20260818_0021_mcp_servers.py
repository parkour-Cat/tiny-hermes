"""MCP servers: a reviewed snapshot of what somebody else's process offers.

Product design §16.2. Two tables and one column worth explaining.

**A snapshot has versions** because the thing being reviewed is the *name set*.
An MCP server's capabilities are not a document somebody uploads — they are
whatever the server answers today — so a version here is the platform writing
down what a person actually looked at: these tool names, these schemas, at this
moment. A binding names that snapshot, which is what makes "the subset an
administrator agreed to" a fact rather than a memory.

**The runtime does not replay it.** §16.2 revalidates the bound subset before
every Run, so what a model is told comes from a fresh `tools/list` restricted
to the bound names. The snapshot fixes which names may be offered; the server
still decides what each one takes. Telling a model a schema the server has
moved on from would be describing a call the far end will reject.

**`last_validated_at`** is when this platform last got an answer out of the
server. "Registered" and "reachable" are different facts and only one of them
is a promise, so the row carries both rather than letting a version's timestamp
stand in for either.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0021"
down_revision: str | None = "20260818_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=48), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_mcp_servers_created_by", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_mcp_servers_workspace_name"),
    )
    op.create_index(
        op.f("ix_mcp_servers_workspace_id"), "mcp_servers", ["workspace_id"], unique=False
    )
    op.create_table(
        "mcp_server_versions",
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'withdrawn')", name="ck_mcp_server_versions_status"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_mcp_server_versions_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mcp_server_id", "content_hash", name="uq_mcp_server_versions_content"),
        sa.UniqueConstraint(
            "mcp_server_id", "version_number", name="uq_mcp_server_versions_number"
        ),
    )
    op.create_index(
        "ix_mcp_server_versions_server_id", "mcp_server_versions", ["mcp_server_id"], unique=False
    )
    # After both tables exist. `use_alter` is what lets a server point at its
    # current version while a version points back at its server.
    op.create_foreign_key(
        "fk_mcp_servers_current_version",
        "mcp_servers",
        "mcp_server_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint("fk_mcp_servers_current_version", "mcp_servers", type_="foreignkey")
    op.drop_index("ix_mcp_server_versions_server_id", table_name="mcp_server_versions")
    op.drop_table("mcp_server_versions")
    op.drop_index(op.f("ix_mcp_servers_workspace_id"), table_name="mcp_servers")
    op.drop_table("mcp_servers")
