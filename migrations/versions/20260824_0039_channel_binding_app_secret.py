"""A Feishu binding learns the secret it authenticates its *own* calls with.

Migration 0037 gave the binding `encrypt_key_ref` (verifies inbound
deliveries) and `app_id`. Replying needs a third value: the app secret,
exchanged with Feishu for a `tenant_access_token` before any message can be
sent back. A reference, not the value — the same shape as `encrypt_key_ref`
and `oidc_providers.client_secret_ref`, so §4.6's "管理元数据,不查看明文"
stays true for anybody who can read this table.

**Nullable, and no CHECK — deliberately, unlike `encrypt_key_ref`.** The
encrypt key gates *receiving*: without it a public webhook accepts forged
deliveries, which is a security hole, so 0037 made it required for Feishu.
The app secret gates *sending*, which is a capability and not a gate: a
binding with no app secret can still receive. That is not a broken state to
forbid — it is exactly what the §929 disconnect-replay drill needs, which
only counts inbound `event_id`s and never replies. Forbidding a
receive-only binding would block the drill this column exists to enable.
The outbound sender refuses to send when it is absent; nothing else is
harmed by its absence.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0039"
down_revision: str | None = "20260822_0038"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_bindings",
        sa.Column("app_secret_ref", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_bindings", "app_secret_ref")
