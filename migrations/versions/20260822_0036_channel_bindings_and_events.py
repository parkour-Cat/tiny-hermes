"""`channel_bindings` and `channel_events`: what a channel publishes, and what it already delivered.

Product §369 names `ChannelBinding` as a domain concept — "Web、飞书和 API 发布
配置" — and §574 names its id as half the deduplication key
(`channel_binding_id + channel_event_id`). Neither table existed, so the key
the product specifies could not be expressed at all.

`channel_events` is written for its **unique constraint**, not for its rows.
Feishu states delivery is at-least-once and retries on a schedule, so the
same `channel_event_id` will arrive more than once and can arrive twice at
the same moment. A read-then-write check loses that race — both deliveries
find nothing and both insert. `uq_channel_events_delivery` is what actually
makes the second delivery a no-op, and `INSERT ... ON CONFLICT DO NOTHING
RETURNING` is how the application asks "was I first?" in one statement.

`run_id` is nullable because the row is claimed *before* the Run exists:
claiming first is what makes the claim exclusive, and a Run created before
the claim would be the duplicate this table exists to prevent.

Retention is 7 days (§574). Unlike `audit_events`, which shipped append-only
with no cleanup and is recorded as a debt, the sweep lands with the table.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0036"
down_revision: str | None = "20260822_0035"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "channel_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        # One binding per channel per Agent per workspace. A second binding
        # for the same trio would give one inbound event two places to land
        # and make "which binding deduplicated this" ambiguous.
        sa.UniqueConstraint(
            "workspace_id", "channel", "agent_id", name="uq_channel_bindings_target"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_channel_bindings_status"
        ),
    )
    op.create_index(
        "ix_channel_bindings_workspace", "channel_bindings", ["workspace_id", "channel"]
    )

    op.create_table(
        "channel_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel_binding_id", sa.Uuid(), nullable=False),
        sa.Column("channel_event_id", sa.String(200), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_binding_id"], ["channel_bindings.id"], ondelete="CASCADE"
        ),
        # No FK to `runs`: §574 keeps this record for 7 days while a Run is
        # kept for as long as the workspace keeps it, so the two lifetimes
        # are independent by design and a constraint here would tie the
        # shorter one to the longer.
        sa.UniqueConstraint(
            "channel_binding_id", "channel_event_id", name="uq_channel_events_delivery"
        ),
    )
    # The sweep's own access path: "everything older than a cutoff", which is
    # a range scan over this column and nothing else.
    op.create_index("ix_channel_events_received_at", "channel_events", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_channel_events_received_at", table_name="channel_events")
    op.drop_table("channel_events")
    op.drop_index("ix_channel_bindings_workspace", table_name="channel_bindings")
    op.drop_table("channel_bindings")
