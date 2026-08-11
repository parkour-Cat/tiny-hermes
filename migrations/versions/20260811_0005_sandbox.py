"""Sandbox reservations and instances.

The partial unique index is the point of this migration. `acquire` must refuse a
Run that already holds a live claim, and a read-then-write check is a race two
Workers can both win. It is partial rather than plain because a Run that
finished with one sandbox and was retried is entitled to another: uniqueness is
over live claims, not over history.

`sandbox_instances` has no host path column and must not gain one — technical
design §6.4, `不保存任意宿主机路径`. Every path the container sees is generated
by the Controller, never round-tripped through a row somebody could write to.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0005"
down_revision: str | None = "20260811_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE = "status IN ('active', 'kept', 'isolated')"


def upgrade() -> None:
    op.create_table(
        "sandbox_instances",
        sa.Column("container_id", sa.String(length=128), nullable=False),
        sa.Column("image_digest", sa.String(length=80), nullable=False),
        sa.Column("resource_profile", sa.String(length=40), nullable=False),
        sa.Column("boot_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "image_digest LIKE 'sha256:%'", name="ck_sandbox_instances_digest"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'frozen', 'isolated', 'destroyed')",
            name="ck_sandbox_instances_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "sandbox_reservations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_instance_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("isolation_reason", sa.String(length=80), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'isolated') OR (isolation_reason IS NULL)",
            name="ck_sandbox_reservations_isolation_reason",
        ),
        sa.CheckConstraint(
            "(status = 'kept') = (idle_expires_at IS NOT NULL)",
            name="ck_sandbox_reservations_keep_deadline",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'kept', 'isolated', 'released')",
            name="ck_sandbox_reservations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_sandbox_reservations_live_run",
        "sandbox_reservations",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_sandbox_reservations_live_run",
        table_name="sandbox_reservations",
        postgresql_where=sa.text(LIVE),
    )
    op.drop_table("sandbox_reservations")
    op.drop_table("sandbox_instances")
