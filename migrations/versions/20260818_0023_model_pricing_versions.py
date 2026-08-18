"""Prices as versions, a Run that fixes one, and a ceiling to measure against.

Product design §12.4, and one distinction the schema exists to preserve.

**No row is not a price of zero.** `model_pricing_versions` holds what an
administrator entered; an endpoint they priced at zero has a row saying so, and
one nobody priced has none. Every nullable money column here means "unknown"
and never "free" — `run_budget_scopes.consumed_cost` most of all, because a
ceiling that met a null and assumed it was satisfied would be a spending limit
that stopped nothing.

**A Run fixes its price.** `runs.model_pricing_version_id` is written at
creation, so an administrator correcting a rate tomorrow does not rewrite what
today's Runs cost. A Run created before anybody entered a price carries null,
which reads as unknown for the rest of its life.

**The ceiling is the workspace's, not the Agent's.** It could not live on
`AgentLimits`: that model is inside the hashed spec, so a new field would put a
key in every published version's normalized document and change every content
hash this platform has written. It is also an operator's decision rather than
an author's, so both reasons point the same way. The workspace's value is
copied onto the Run at creation, so the valve cannot move underneath a Run that
is already being measured against it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0023"
down_revision: str | None = "20260818_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_pricing_versions",
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("input_per_million", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("output_per_million", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("cached_input_per_million", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(currency) = 3", name="ck_model_pricing_versions_currency"),
        sa.CheckConstraint(
            "input_per_million >= 0 AND output_per_million >= 0 AND "
            "(cached_input_per_million IS NULL OR cached_input_per_million >= 0)",
            name="ck_model_pricing_versions_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_model_pricing_versions_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["model_endpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "endpoint_id", "version_number", name="uq_model_pricing_versions_number"
        ),
    )
    op.create_index(
        "ix_model_pricing_versions_endpoint",
        "model_pricing_versions",
        ["endpoint_id", "effective_at"],
        unique=False,
    )
    op.add_column(
        "run_budget_scopes", sa.Column("max_cost", sa.Numeric(precision=20, scale=6), nullable=True)
    )
    op.add_column(
        "run_budget_scopes", sa.Column("cost_currency", sa.String(length=3), nullable=True)
    )
    op.add_column(
        "run_budget_scopes",
        sa.Column("consumed_cost", sa.Numeric(precision=20, scale=6), nullable=True),
    )
    op.add_column(
        "run_budget_scopes",
        sa.Column("cost_quality", sa.String(length=16), server_default="unknown", nullable=False),
    )
    op.add_column("runs", sa.Column("model_pricing_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "workspaces", sa.Column("max_run_cost", sa.Numeric(precision=20, scale=6), nullable=True)
    )
    op.add_column("workspaces", sa.Column("cost_currency", sa.String(length=3), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_cost_ceiling_non_negative",
        "workspaces",
        "max_run_cost IS NULL OR max_run_cost >= 0",
    )
    op.create_check_constraint(
        "ck_workspaces_cost_ceiling_paired",
        "workspaces",
        "(max_run_cost IS NULL) = (cost_currency IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_cost_ceiling_paired", "workspaces", type_="check")
    op.drop_constraint("ck_workspaces_cost_ceiling_non_negative", "workspaces", type_="check")
    op.drop_column("workspaces", "cost_currency")
    op.drop_column("workspaces", "max_run_cost")
    op.drop_column("runs", "model_pricing_version_id")
    op.drop_column("run_budget_scopes", "cost_quality")
    op.drop_column("run_budget_scopes", "consumed_cost")
    op.drop_column("run_budget_scopes", "cost_currency")
    op.drop_column("run_budget_scopes", "max_cost")
    op.drop_index("ix_model_pricing_versions_endpoint", table_name="model_pricing_versions")
    op.drop_table("model_pricing_versions")
