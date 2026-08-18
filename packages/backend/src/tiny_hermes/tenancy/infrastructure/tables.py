from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class WorkspaceRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        # §16.3's own range. Enforced here as well as clamped in the domain:
        # the constraint stops an impossible value from being stored, and the
        # clamp stops one that somehow was from failing a Run.
        CheckConstraint(
            "approval_validity_seconds IS NULL OR "
            "approval_validity_seconds BETWEEN 300 AND 604800",
            name="ck_workspaces_approval_validity",
        ),
    )

    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="active")
    #: How long an approval this workspace asks for stays good, in seconds.
    #: Null means the platform default of 24 hours (§16.3). Kept as a column
    #: here rather than in a settings table because it is the only per-
    #: workspace number so far, and one column is cheaper to read than a table
    #: nobody else uses.
    approval_validity_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )


class MembershipRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32))
