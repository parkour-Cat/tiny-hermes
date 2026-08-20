"""Two tables, shaped like the HTTP tool catalog's.

The one column that has no counterpart there is `last_validated_at`. §16.2
requires the bound subset to be revalidated before every Run, and an
administrator looking at a list of servers needs to know which of them this
platform has actually reached — "registered" and "reachable" are different
facts, and only one of them is a promise.
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


class McpServerRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_mcp_servers_workspace_name"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(48))
    url: Mapped[str] = mapped_column(String(2048))
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "mcp_server_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_mcp_servers_current_version",
        ),
        nullable=True,
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_mcp_servers_created_by")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class McpServerVersionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "mcp_server_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'withdrawn')", name="ck_mcp_server_versions_status"
        ),
        UniqueConstraint(
            "mcp_server_id", "content_hash", name="uq_mcp_server_versions_content"
        ),
        UniqueConstraint(
            "mcp_server_id", "version_number", name="uq_mcp_server_versions_number"
        ),
        Index("ix_mcp_server_versions_server_id", "mcp_server_id"),
    )

    mcp_server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE")
    )
    version_number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    #: The reviewed snapshot: name, description and input schema per tool.
    tools: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_mcp_server_versions_created_by"
        )
    )
