"""One row of `audit_events`, read back.

A plain mirror of `AuditEventRow` (`audit/infrastructure/tables.py`) rather
than anything with its own behaviour — the interesting decisions in this
module live in `scope.py` and `redaction.py`, both of which operate on this
shape rather than on the ORM row directly, so a store implementation that is
not SQLAlchemy (a fake for a unit test, say) has something to construct.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AuditRecord:
    id: UUID
    workspace_id: UUID | None
    actor_type: str
    actor_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID | None
    result: str
    request_id: str
    context: dict[str, Any]
    created_at: datetime
