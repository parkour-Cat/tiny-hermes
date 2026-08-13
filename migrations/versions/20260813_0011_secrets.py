"""Secrets stored as ciphertext under a wrapped DEK.

The table holds the envelope, never the plaintext. A dump of this table without
the matching KEK cannot decrypt. Unique names are per workspace for workspace
secrets and global for platform secrets; PostgreSQL unique constraints treat
NULL as distinct, so those rules are partial unique indexes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0011"
down_revision: str | None = "20260813_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("mask", sa.String(length=200), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('workspace', 'platform')", name="ck_secrets_scope"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_secrets_status"),
        sa.CheckConstraint(
            "(scope = 'platform' AND workspace_id IS NULL) OR "
            "(scope = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_secrets_scope_workspace",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_secrets_workspace_id", "secrets", ["workspace_id"])
    op.create_index(
        "uq_secrets_workspace_name",
        "secrets",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("scope = 'workspace'"),
    )
    op.create_index(
        "uq_secrets_platform_name",
        "secrets",
        ["name"],
        unique=True,
        postgresql_where=sa.text("scope = 'platform'"),
    )


def downgrade() -> None:
    op.drop_index("uq_secrets_platform_name", table_name="secrets")
    op.drop_index("uq_secrets_workspace_name", table_name="secrets")
    op.drop_index("ix_secrets_workspace_id", table_name="secrets")
    op.drop_table("secrets")
