from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from tiny_hermes.secrets.domain.models import SecretScope, SecretStatus
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


def _in_enum(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


class SecretRow(IdMixin, CreatedAtMixin, Base):
    """Ciphertext under a wrapped DEK. There is no plaintext column."""

    __tablename__ = "secrets"
    __table_args__ = (
        CheckConstraint(_in_enum("scope", SecretScope), name="ck_secrets_scope"),
        CheckConstraint(_in_enum("status", SecretStatus), name="ck_secrets_status"),
        CheckConstraint(
            "(scope = 'platform' AND workspace_id IS NULL) OR "
            "(scope = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_secrets_scope_workspace",
        ),
        Index(
            "uq_secrets_workspace_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("scope = 'workspace'"),
        ),
        Index(
            "uq_secrets_platform_name",
            "name",
            unique=True,
            postgresql_where=text("scope = 'platform'"),
        ),
    )

    name: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=SecretStatus.ACTIVE.value)
    mask: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    wrap_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
