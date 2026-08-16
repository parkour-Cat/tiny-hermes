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

from tiny_hermes.identity.domain.models import API_KEY_SCOPES, ServiceAccountStatus
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
    password_hash: Mapped[str] = mapped_column(String(512))


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
