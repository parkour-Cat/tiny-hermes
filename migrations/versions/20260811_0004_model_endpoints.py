"""Add the platform-scoped model endpoint registry."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0004"
down_revision: str | None = "20260810_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_endpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_quality", sa.String(length=20), nullable=False),
        # A name, never a value: the environment variable the deployment
        # supplies the credential through. Nothing here holds a secret, and no
        # column is reserved for one — Secret storage is phase four, and a
        # column standing empty until then would read as though it were already
        # doing something.
        sa.Column("credential_ref", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_model_endpoints_name"),
        sa.CheckConstraint("kind IN ('openai_compatible')", name="ck_model_endpoints_kind"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_model_endpoints_status"
        ),
        # `estimated` is excluded here as well as in the schema. A Token count
        # nothing can stand behind should not be storable by any path.
        sa.CheckConstraint(
            "usage_quality IN ('provider', 'unavailable')", name="ck_model_endpoints_usage"
        ),
        sa.CheckConstraint("context_window > 0", name="ck_model_endpoints_context_window"),
        sa.CheckConstraint(
            "max_output_tokens > 0 AND max_output_tokens <= context_window",
            name="ck_model_endpoints_max_output",
        ),
    )


def downgrade() -> None:
    op.drop_table("model_endpoints")
