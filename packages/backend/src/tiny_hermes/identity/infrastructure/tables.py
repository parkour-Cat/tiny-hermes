from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.identity.domain.models import (
    API_KEY_SCOPES,
    OidcProviderStatus,
    ServiceAccountStatus,
)
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


def _in_enum(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


_SCOPE_LITERAL = "[" + ", ".join(f'"{name}"' for name in sorted(API_KEY_SCOPES)) + "]"


class UserRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    status: Mapped[str] = mapped_column(String(32), default="active")
    display_name: Mapped[str] = mapped_column(String(120))
    is_platform_admin: Mapped[bool] = mapped_column(default=False)


class AuthIdentityRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(320))
    #: Nullable since migration 0035: an `oidc` identity authenticates through
    #: the IdP's own exchange and never has a password of its own. A `local`
    #: identity always has one — nothing creates one without — but the column
    #: cannot say that on its own, which is why `AuthService.login` checks for
    #: `None` explicitly rather than trusting this constraint alone.
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)


class AuthSessionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "auth_sessions"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceAccountRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "service_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_service_accounts_workspace_name"),
        CheckConstraint(
            "role IN ('developer', 'viewer')",
            name="ck_service_accounts_role",
        ),
        CheckConstraint(
            _in_enum("status", ServiceAccountStatus),
            name="ck_service_accounts_status",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=ServiceAccountStatus.ACTIVE.value)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class ApiKeyRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_api_keys_token_digest"),
        Index("ix_api_keys_prefix", "prefix"),
        CheckConstraint(
            "jsonb_typeof(CAST(scopes AS jsonb)) = 'array' AND "
            f"CAST(scopes AS jsonb) <@ '{_SCOPE_LITERAL}'::jsonb",
            name="ck_api_keys_scopes",
        ),
    )

    service_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("service_accounts.id", ondelete="CASCADE"), index=True
    )
    token_digest: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(8))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    agent_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OidcProviderRow(IdMixin, CreatedAtMixin, Base):
    """OIDC login design §1. `client_secret_ref` names either an environment
    variable or the id of an active `secrets` row — the same two-shape
    reference `ModelEndpointRow.credential_ref` already uses
    (`model_catalog/infrastructure/credentials.py`) — never a plaintext
    column. Resolved at call time by `CredentialResolver` and stored nowhere
    else."""

    __tablename__ = "oidc_providers"
    __table_args__ = (
        UniqueConstraint("issuer", name="uq_oidc_providers_issuer"),
        CheckConstraint(_in_enum("status", OidcProviderStatus), name="ck_oidc_providers_status"),
    )

    issuer: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret_ref: Mapped[str] = mapped_column(String(200))
    discovery_url: Mapped[str] = mapped_column(String(500))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default=OidcProviderStatus.ACTIVE.value)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class OidcLoginStateRow(IdMixin, CreatedAtMixin, Base):
    """One `/start` → one row: `state` and `nonce` (both CSRF/replay guards
    per design §2) plus the PKCE `code_verifier`, held server-side rather
    than in a cookie so a callback cannot be completed with anything the
    browser carried on its own. `consumed_at` makes `state` single-use —
    `SqlOidcProviderStore.consume_login_state` sets it with the same
    `UPDATE ... WHERE consumed_at IS NULL RETURNING` shape a race cannot
    beat, so a replayed `state` finds no row the second time."""

    __tablename__ = "oidc_login_states"

    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("oidc_providers.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(128), unique=True)
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    redirect_uri: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
