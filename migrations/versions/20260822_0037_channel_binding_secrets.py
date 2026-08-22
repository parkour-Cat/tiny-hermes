"""A Feishu binding learns which key its deliveries are signed with.

`channel_bindings` (migration 0036) carried only what every channel has.
Feishu's Webhook transport needs two tenant-specific values, and both are
secrets, so they land here as **references** rather than values — the same
two-shape reference `oidc_providers.client_secret_ref` and
`model_endpoints.credential_ref` already use, resolved by
`CredentialResolver` at the moment of use. §4.6's 密钥 row is "管理元数据,
不查看明文", and a column holding the key itself would make that false for
anybody who can read this table.

The CHECK is the interesting part. A Feishu binding whose `encrypt_key_ref`
is NULL would accept **unencrypted, unsigned** deliveries from anyone who
learned the URL — a webhook endpoint is public by construction, so the
signature is the only thing standing between it and the internet. Feishu
permits plaintext callbacks; this platform does not, and the constraint is
what makes that a fact about the schema rather than a habit of whatever
code happens to read it.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0037"
down_revision: str | None = "20260822_0036"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "channel_bindings", sa.Column("encrypt_key_ref", sa.String(200), nullable=True)
    )
    op.add_column(
        "channel_bindings", sa.Column("app_id", sa.String(120), nullable=True)
    )
    op.create_check_constraint(
        "ck_channel_bindings_feishu_is_encrypted",
        "channel_bindings",
        "channel <> 'feishu' OR encrypt_key_ref IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_channel_bindings_feishu_is_encrypted", "channel_bindings", type_="check"
    )
    op.drop_column("channel_bindings", "app_id")
    op.drop_column("channel_bindings", "encrypt_key_ref")
