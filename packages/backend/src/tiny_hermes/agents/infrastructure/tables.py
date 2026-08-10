from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin

AGENT_STATUSES = ("draft", "published")


def _in_values(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


class AgentRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "alias", name="uq_agents_workspace_alias"),
        UniqueConstraint("id", "workspace_id", name="uq_agents_id_workspace"),
        CheckConstraint(_in_values("status", AGENT_STATUSES), name="ck_agents_status"),
        ForeignKeyConstraint(
            ["current_version_id", "workspace_id"],
            ["agent_versions.id", "agent_versions.workspace_id"],
            name="fk_agents_current_version",
            use_alter=True,
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    alias: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)


class AgentDraftRow(Base):
    __tablename__ = "agent_drafts"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_agent_drafts_revision_positive"),
    )

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AgentVersionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_number"),
        UniqueConstraint("id", "workspace_id", name="uq_agent_versions_id_workspace"),
        CheckConstraint(
            "version_number > 0", name="ck_agent_versions_number_positive"
        ),
        ForeignKeyConstraint(
            ["agent_id", "workspace_id"],
            ["agents.id", "agents.workspace_id"],
            name="fk_agent_versions_agent",
            ondelete="CASCADE",
        ),
    )

    agent_id: Mapped[UUID] = mapped_column(index=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    published_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
