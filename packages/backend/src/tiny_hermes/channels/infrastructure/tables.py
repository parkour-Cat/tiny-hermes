"""Where a channel publishes an Agent, and what it has already delivered.

See migration `20260822_0036` for why `channel_events` exists for its unique
constraint rather than for its rows.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class ChannelBindingRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "channel_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "channel", "agent_id", name="uq_channel_bindings_target"
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_channel_bindings_status"
        ),
        Index("ix_channel_bindings_workspace", "workspace_id", "channel"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class ChannelEventRow(IdMixin, Base):
    __tablename__ = "channel_events"
    __table_args__ = (
        UniqueConstraint(
            "channel_binding_id", "channel_event_id", name="uq_channel_events_delivery"
        ),
        # The sweep's only access path: everything older than a cutoff.
        Index("ix_channel_events_received_at", "received_at"),
    )

    channel_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("channel_bindings.id", ondelete="CASCADE")
    )
    channel_event_id: Mapped[str] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Nullable because the row is claimed *before* the Run exists — claiming
    #: first is what makes the claim exclusive. A Run created before the claim
    #: would be the duplicate this table exists to prevent.
    run_id: Mapped[UUID | None] = mapped_column(nullable=True)
