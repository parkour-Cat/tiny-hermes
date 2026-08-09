from typing import Any
from uuid import UUID

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class AuditEventRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_events"

    workspace_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
