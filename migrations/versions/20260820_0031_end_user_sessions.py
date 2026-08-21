"""The platform's own credential store for the third subject.

End-user entry design §4.2–§4.3. Deliberately its own table rather than a
`caller_type` branch on `auth_sessions`: the two identity systems sharing a
session table is exactly the shape the design's third red line rules out —
one credential a platform-member code path could end up trusting for an end
user, or vice versa. A separate migration for a separate table keeps that
true at the schema level, not just in review.

`fk_end_user_sessions_end_user` is a composite FK against
`(end_users.id, end_users.workspace_id)`, mirroring 0030's
`fk_external_identities_end_user` — the revocation endpoint takes a bare
`end_user_id` and a workspace from the caller's own session, and this is what
lets the query trust that combination without a second lookup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0031"
down_revision: str | None = "20260820_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "end_user_sessions",
        sa.Column("end_user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["end_user_id", "workspace_id"],
            ["end_users.id", "end_users.workspace_id"],
            name="fk_end_user_sessions_end_user",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_end_user_sessions_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_end_user_sessions_token_digest"),
    )
    op.create_index("ix_end_user_sessions_end_user_id", "end_user_sessions", ["end_user_id"])
    op.create_index("ix_end_user_sessions_workspace_id", "end_user_sessions", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_end_user_sessions_workspace_id", table_name="end_user_sessions")
    op.drop_index("ix_end_user_sessions_end_user_id", table_name="end_user_sessions")
    op.drop_table("end_user_sessions")
