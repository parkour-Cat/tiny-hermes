"""Two tables. The document is a column, not an object in storage.

An OpenAPI export is small text, and one table makes immutability, the content
hash and per-version rollback a single foreign key rather than two stores to
reconcile — the same reasoning `skill_files` records.

Both the parsed operations and the original body are kept. The first is what a
binding is checked against and what the model is told about; the second is what
an administrator compares when they want to know what actually changed.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.identity.infrastructure import tables as identity_tables  # noqa: F401
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin
from tiny_hermes.tenancy.infrastructure import tables as tenancy_tables  # noqa: F401

#: The tables this module's foreign keys name, imported for the reason
#: `skills/infrastructure/tables.py` gives: a key resolves by table name at
#: flush time, and not every process imports the whole platform.
REFERENCED_TABLE_MODULES = (identity_tables, tenancy_tables)


class HttpToolRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "http_tools"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_http_tools_workspace_name"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(48))
    base_url: Mapped[str] = mapped_column(String(2048))
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("http_tool_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HttpToolVersionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "http_tool_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'withdrawn')", name="ck_http_tool_versions_status"
        ),
        UniqueConstraint(
            "http_tool_id", "content_hash", name="uq_http_tool_versions_content"
        ),
        UniqueConstraint(
            "http_tool_id", "version_number", name="uq_http_tool_versions_number"
        ),
        Index("ix_http_tool_versions_tool_id", "http_tool_id"),
    )

    http_tool_id: Mapped[UUID] = mapped_column(
        ForeignKey("http_tools.id", ondelete="CASCADE")
    )
    version_number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    document_version: Mapped[str] = mapped_column(String(64))
    operations: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    document: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
