"""Where an approved target is written down.

One table for both levels rather than two, the shape `skills` already uses: a
`scope` column with `workspace_id` null exactly when it says `platform`, and a
CHECK that says so. Two tables would have meant two of every query and two
chances for the platform level to gain a rule the workspace level did not.

The uniqueness is per level and per entry, so a workspace may approve
`api.example.com` while the platform also does. That is not redundancy — the
workspace's entry is what survives the intersection when the platform later
widens or narrows its own.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.identity.infrastructure import tables as identity_tables  # noqa: F401
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin
from tiny_hermes.tenancy.infrastructure import tables as tenancy_tables  # noqa: F401

#: Same reason `skills/infrastructure/tables.py` names its neighbours: a
#: foreign key resolves by table name at flush time, and the process writing
#: these rows may not be the one that imported every module.
REFERENCED_TABLE_MODULES = (identity_tables, tenancy_tables)


class ScopeLevel(StrEnum):
    PLATFORM = "platform"
    WORKSPACE = "workspace"


class OutboundScopeRow(IdMixin, CreatedAtMixin, Base):
    """One approved host, wildcard or network, at one level."""

    __tablename__ = "outbound_scopes"
    __table_args__ = (
        CheckConstraint(
            "level IN ('platform', 'workspace')", name="ck_outbound_scopes_level"
        ),
        CheckConstraint(
            "(level = 'platform' AND workspace_id IS NULL) OR "
            "(level = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_outbound_scopes_level_workspace",
        ),
        Index(
            "uq_outbound_scopes_workspace_entry",
            "workspace_id",
            "entry",
            unique=True,
            postgresql_where=text("level = 'workspace'"),
        ),
        Index(
            "uq_outbound_scopes_platform_entry",
            "entry",
            unique=True,
            postgresql_where=text("level = 'platform'"),
        ),
    )

    level: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: Stored normalized — lowercase host, or a network in its canonical form —
    #: so the uniqueness above is uniqueness of meaning rather than of spelling.
    entry: Mapped[str] = mapped_column(String(255))
    #: Why somebody approved it. Optional, and worth having: an entry nobody
    #: remembers the reason for is an entry nobody dares remove.
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    #: Set when this entry belongs to a model endpoint rather than to a person.
    #: Registering an endpoint approves its host; disabling one takes the
    #: approval away again, and neither is a thing an administrator has to
    #: remember to do twice.
    endpoint_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
