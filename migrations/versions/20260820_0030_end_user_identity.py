"""The identity skeleton for somebody the platform does not authenticate.

End-user entry design §3. Three tables land together because none of them is
useful alone: a subject with nothing to map a credential onto, a mapping with
no subject to point at, and an issuer registry nobody consults yet are three
half-features. `CallerType` grows its third member in the same migration for
the same reason — a `sessions.caller_type` value that could not yet be written
would be dead the moment it landed.

`sessions.caller_type` and `idempotency_records.caller_type` are the two CHECK
constraints `_in_enum(CallerType)` generates elsewhere in the schema (design
§3: "`sessions.caller_type='end_user'` 时 `caller_id` 指向 `end_users.id`").
Both are dropped and recreated rather than left alone, because a CHECK a later
migration widens is a CHECK this one would otherwise have silently pinned to
two values forever.

The downgrade drops the three new tables first — nothing yet references them,
so there is no data loss to reason about — then narrows the two CHECKs back.
Narrowing before any row can hold `'end_user'` needs no `DELETE`, unlike 0015's
downgrade: this feature has no producer yet, so 0029's state is still exactly
reachable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0030"
down_revision: str | None = "20260819_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CALLER_TYPES_BEFORE = "caller_type IN ('user', 'service_account')"
CALLER_TYPES_AFTER = "caller_type IN ('user', 'service_account', 'end_user')"


def upgrade() -> None:
    op.create_table(
        "end_users",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_end_users_workspace", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_end_users_id_workspace"),
    )
    op.create_index("ix_end_users_workspace_id", "end_users", ["workspace_id"])

    op.create_table(
        "external_identities",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("end_user_id", sa.Uuid(), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["end_user_id", "workspace_id"],
            ["end_users.id", "end_users.workspace_id"],
            name="fk_external_identities_end_user",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_external_identities_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        # §282: the identity this whole design rests on. The same `sub` from
        # the same channel in the same workspace must always resolve to the
        # same end user, or a second credential exchange creates a stranger.
        sa.UniqueConstraint(
            "workspace_id",
            "channel",
            "external_user_id",
            name="uq_external_identities_workspace_channel_external_user",
        ),
    )
    op.create_index("ix_external_identities_workspace_id", "external_identities", ["workspace_id"])
    op.create_index("ix_external_identities_end_user_id", "external_identities", ["end_user_id"])

    op.create_table(
        "channel_issuers",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("public_key", sa.String(), nullable=True),
        sa.Column("jwks_url", sa.String(length=2048), nullable=True),
        sa.Column("allowed_origins", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_channel_issuers_status"),
        sa.CheckConstraint(
            "public_key IS NOT NULL OR jwks_url IS NOT NULL",
            name="ck_channel_issuers_key_source",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_channel_issuers_created_by"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_channel_issuers_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "channel", "issuer", name="uq_channel_issuers_workspace_channel_issuer"
        ),
    )
    op.create_index("ix_channel_issuers_workspace_id", "channel_issuers", ["workspace_id"])

    op.drop_constraint("ck_sessions_caller_type", "sessions", type_="check")
    op.create_check_constraint("ck_sessions_caller_type", "sessions", CALLER_TYPES_AFTER)
    op.drop_constraint("ck_idempotency_records_caller_type", "idempotency_records", type_="check")
    op.create_check_constraint(
        "ck_idempotency_records_caller_type", "idempotency_records", CALLER_TYPES_AFTER
    )


def downgrade() -> None:
    op.drop_constraint("ck_idempotency_records_caller_type", "idempotency_records", type_="check")
    op.create_check_constraint(
        "ck_idempotency_records_caller_type", "idempotency_records", CALLER_TYPES_BEFORE
    )
    op.drop_constraint("ck_sessions_caller_type", "sessions", type_="check")
    op.create_check_constraint("ck_sessions_caller_type", "sessions", CALLER_TYPES_BEFORE)

    op.drop_index("ix_channel_issuers_workspace_id", table_name="channel_issuers")
    op.drop_table("channel_issuers")

    op.drop_index("ix_external_identities_end_user_id", table_name="external_identities")
    op.drop_index("ix_external_identities_workspace_id", table_name="external_identities")
    op.drop_table("external_identities")

    op.drop_index("ix_end_users_workspace_id", table_name="end_users")
    op.drop_table("end_users")
