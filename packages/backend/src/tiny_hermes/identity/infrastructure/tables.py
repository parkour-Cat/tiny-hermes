from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


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
