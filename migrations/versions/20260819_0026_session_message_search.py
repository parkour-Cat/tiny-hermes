"""Session messages gain the index §14.3's search runs on.

A generated `tsvector` over the message's **text parts**, not over the stored
document: a search should match what somebody said, not the JSON shape it was
kept in, and indexing the whole document would match on `"type"` and `"parts"`
for every row.

`simple` rather than a stemmed configuration, the same choice `memories` makes
and for the same reason — this platform serves Chinese and English side by side,
and a stemmer for one silently mangles the other. Keyword matching is what §14.3
promises, and `simple` is what keyword matching actually is.

Backfilled by the database rather than by this migration: a stored generated
column is computed for every existing row when it is added, so a deployment with
a year of conversations comes out searchable without a data step of its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260819_0026"
down_revision: str | None = "20260819_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_messages",
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', "
                "jsonb_path_query_array(content::jsonb, '$.parts[*].text')::text)",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_session_messages_search",
        "session_messages",
        ["search"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_messages_search", table_name="session_messages", postgresql_using="gin"
    )
    op.drop_column("session_messages", "search")
