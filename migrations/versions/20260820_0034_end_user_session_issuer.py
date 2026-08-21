"""`end_user_sessions` learns which issuer minted it.

Task-7 review finding 3. `SqlEndUserStore.active_allowed_origins` unioned
`allowed_origins` across every *active* `channel_issuers` row in a
workspace, when design §7's own wording is "that issuer's registered
origins" — a workspace with two active issuers let a page served from
issuer B's registered origin act on a session minted through issuer A's
credential. The reason task 7 gave was that `end_user_sessions` carried no
record of which issuer signed the credential it was exchanged from (design's
own red line: the credential itself is gone the moment it is exchanged, so
nothing downstream could re-derive it).

`channel_issuer_id` is that record, set once at exchange time
(`EndUserIdentityService.exchange`) and never touched again. Nullable
because a session minted before this column existed has no issuer to
backfill — the application layer treats a `NULL` here as "this session's
origin cannot be verified" and refuses its cross-origin writes rather than
falling back to the old, over-permissive union (see
`EndUserIdentityService.allowed_origins_for_issuer`'s own docstring). `ON
DELETE SET NULL` rather than `RESTRICT`: there is no delete endpoint for a
`channel_issuers` row today, but if one is ever added, a session outliving
the issuer that minted it should degrade to "unverifiable" the same way a
pre-migration session does, not block the deletion.

A plain (non-composite) FK to `channel_issuers.id` is enough for tenant
safety here: the application query that reads this column always filters by
`workspace_id` too (`SqlEndUserStore.allowed_origins_for_issuer`), the same
way `ChannelIssuerRow.id` is already a globally unique UUID primary key —
there is no second table with an ambiguous `id` this could be confused with.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0034"
down_revision: str | None = "20260820_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "end_user_sessions",
        sa.Column("channel_issuer_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_end_user_sessions_channel_issuer_id",
        "end_user_sessions",
        ["channel_issuer_id"],
    )
    op.create_foreign_key(
        "fk_end_user_sessions_channel_issuer",
        "end_user_sessions",
        "channel_issuers",
        ["channel_issuer_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_end_user_sessions_channel_issuer", "end_user_sessions", type_="foreignkey"
    )
    op.drop_index("ix_end_user_sessions_channel_issuer_id", table_name="end_user_sessions")
    op.drop_column("end_user_sessions", "channel_issuer_id")
