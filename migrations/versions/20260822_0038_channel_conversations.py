"""Which Session a channel user is talking in.

Without this every inbound message would start a Session of its own. Private
memory would survive that — it is scoped to the subject, not the Session —
but the conversation would not, and both §19.2's completion notifications
and §497's blocked-head card assume there is a thread to answer *in*.

Keyed by `(binding, external_user_id)` rather than by Feishu's `chat_id`:
the subject is the person, and §282 already makes
`(workspace, channel, external_user_id)` the identity. A `chat_id` key would
give the same person two threads in a group chat and a direct message, which
is a product decision nobody has made.

Separate from `channel_events` because the lifetimes differ by design:
§574 sweeps a delivery record at seven days, and a conversation must outlive
that or a quiet fortnight would silently start someone over.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0038"
down_revision: str | None = "20260822_0037"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "channel_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel_binding_id", sa.Uuid(), nullable=False),
        sa.Column("external_user_id", sa.String(200), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_binding_id"], ["channel_bindings.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "channel_binding_id",
            "external_user_id",
            name="uq_channel_conversations_participant",
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_conversations")
