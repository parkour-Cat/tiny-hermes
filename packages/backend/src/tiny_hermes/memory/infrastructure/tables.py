"""One table for both kinds of memory, and the index the whole phase reads by.

Private and shared live together because they differ by *who* rather than by
what: the same body, the same lifecycle, the same relevance ranking. Two tables
would mean two places to get the isolation rule right, and the isolation rule
is the one that must not be got wrong twice.

`search` is a generated `tsvector` with a GIN index over it. Session search
(§14.3) and the memory segment's relevance ranking use the same index on
purpose: two would give two answers to "how relevant is this", and a person
comparing what they searched with what the Agent remembered would find them
disagreeing.

**The subject columns are not nullable together by accident.** `subject_type`
and `subject_id` are both null exactly when the row is shared, and a CHECK says
so — a private row with no owner and a shared row with one are both silent
mistakes, and both are the leak §14.1 exists to prevent.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.agents.infrastructure import tables as agent_tables  # noqa: F401
from tiny_hermes.identity.infrastructure import tables as identity_tables  # noqa: F401
from tiny_hermes.memory.domain.scope import MemoryStatus
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin
from tiny_hermes.tenancy.infrastructure import tables as tenancy_tables  # noqa: F401

#: The tables this module's foreign keys name, imported for the reason
#: `skills/infrastructure/tables.py` gives: a key resolves by table name at
#: flush time, and not every process imports the whole platform.
REFERENCED_TABLE_MODULES = (agent_tables, identity_tables, tenancy_tables)

#: The text search configuration. `simple` rather than `english`: this platform
#: serves Chinese and English side by side, and a stemmer for one language
#: silently mangles the other. Keyword matching is what §14.3 promises, and
#: `simple` is what keyword matching actually is.
SEARCH_CONFIG = "simple"


class MemoryRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('private', 'shared')", name="ck_memories_kind"
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'rejected')", name="ck_memories_status"
        ),
        CheckConstraint(
            "(kind = 'private') = (subject_type IS NOT NULL AND subject_id IS NOT NULL)",
            name="ck_memories_subject_matches_kind",
        ),
        CheckConstraint(
            "subject_type IS NULL OR subject_type IN "
            "('user', 'service_account', 'end_user')",
            name="ck_memories_subject_type",
        ),
        # The read path's index, in the order it filters: a Run asks for one
        # scope's active rows and nothing else ever asks for anything wider.
        Index(
            "ix_memories_scope",
            "workspace_id",
            "agent_id",
            "kind",
            "subject_type",
            "subject_id",
            "status",
        ),
        Index("ix_memories_search", "search", postgresql_using="gin"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    #: Null together, exactly when the row is shared. See the CHECK above.
    subject_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject_id: Mapped[UUID | None] = mapped_column(nullable=True)
    body: Mapped[str] = mapped_column(Text)
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{SEARCH_CONFIG}', body)", persisted=True),
    )
    status: Mapped[str] = mapped_column(
        String(16), default=MemoryStatus.PENDING.value
    )
    #: Where this came from: a Run that proposed it, or a person who wrote it.
    #: Kept because a memory nobody can trace is one nobody can correct.
    origin: Mapped[str] = mapped_column(String(32))
    origin_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    #: The approval this candidate is waiting on, when it is waiting on one.
    #: Not a foreign key for the reason `outbound_scopes.endpoint_id` is not:
    #: an approval may be swept away by retention long after the memory it
    #: admitted is still in force.
    approval_id: Mapped[UUID | None] = mapped_column(nullable=True)
    #: Free-form provenance a reviewer reads. Never the raw message a candidate
    #: came from — §14.2's rule, and this platform extends it to private.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_memories_created_by"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
