"""OIDC login design §1: `oidc_providers`, `oidc_login_states`, and the one
schema change the flow needs on an existing table.

Two tables land together because, like 0030's three, neither is useful
alone: a provider registry with no flow to drive and a login-state table
with no provider to point at are two half-features. `oidc_login_states`
holds `state`/`nonce`/PKCE `code_verifier` server-side rather than in a
cookie (design §2's own line: "不放 cookie 里") — a callback that only had
what the browser carried on its own would be trusting the one channel an
attacker controls.

`auth_identities.password_hash` becomes nullable in the same migration
because it is the one existing column this feature cannot satisfy: an OIDC
identity authenticates through the IdP's own exchange and never has a
password `AuthService.login` could hash and compare. Widening it here rather
than in a follow-up keeps `oidc_providers.upgrade()` — the first migration
that can actually produce a null in this column — the same transaction that
makes null legal. `AuthService.login` refuses an identity whose
`password_hash` is `None` explicitly (never trusts the constraint alone,
since a constraint only says what the database will accept, not what the
service does with it) — see its own test for the reasoning.

The downgrade narrows `password_hash` back to `NOT NULL` before dropping the
new tables. That ordering matters the way it never did for 0030: this
feature's downgrade path is reachable with a null already written (a real
OIDC user, not merely an unused table), so narrowing blind would fail on
that row. A deployment downgrading past this revision is expected to have
first removed or backfilled any OIDC-only identity — the same trade every
other "make a new column mandatory again" migration in this codebase makes,
stated here rather than discovered as a downgrade failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0035"
down_revision: str | None = "20260820_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "auth_identities",
        "password_hash",
        existing_type=sa.String(length=512),
        nullable=True,
    )

    op.create_table(
        "oidc_providers",
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret_ref", sa.String(length=200), nullable=False),
        sa.Column("discovery_url", sa.String(length=500), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_oidc_providers_status"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_oidc_providers_created_by"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", name="uq_oidc_providers_issuer"),
    )

    op.create_table(
        "oidc_login_states",
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("redirect_uri", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["oidc_providers.id"],
            name="fk_oidc_login_states_provider",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state", name="uq_oidc_login_states_state"),
    )
    op.create_index(
        "ix_oidc_login_states_provider_id", "oidc_login_states", ["provider_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_oidc_login_states_provider_id", table_name="oidc_login_states")
    op.drop_table("oidc_login_states")
    op.drop_table("oidc_providers")
    op.alter_column(
        "auth_identities",
        "password_hash",
        existing_type=sa.String(length=512),
        nullable=False,
    )
