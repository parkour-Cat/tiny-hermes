"""Service accounts and hashed API keys.

A workspace-scoped calling principal that is not a User, and the keys that
authenticate as it. The plaintext token never has a column: only the SHA-256
digest is stored, and the prefix exists so a listing can identify a key the
caller already saw once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0009"
down_revision: str | None = "20260811_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPES = '["agents.read", "runs.control", "runs.read", "runs.write"]'


def upgrade() -> None:
    op.create_table(
        "service_accounts",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('developer', 'viewer')", name="ck_service_accounts_role"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_service_accounts_status"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_service_accounts_workspace_name"
        ),
    )
    op.create_index(
        "ix_service_accounts_workspace_id", "service_accounts", ["workspace_id"]
    )
    op.create_table(
        "api_keys",
        sa.Column("service_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("agent_ids", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(CAST(scopes AS jsonb)) = 'array' AND "
            f"CAST(scopes AS jsonb) <@ '{SCOPES}'::jsonb",
            name="ck_api_keys_scopes",
        ),
        sa.ForeignKeyConstraint(
            ["service_account_id"], ["service_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_api_keys_token_digest"),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_index("ix_api_keys_service_account_id", "api_keys", ["service_account_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_service_account_id", table_name="api_keys")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_service_accounts_workspace_id", table_name="service_accounts")
    op.drop_table("service_accounts")
