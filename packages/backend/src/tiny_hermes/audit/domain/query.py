"""What one page of `audit_events` may ask for.

Mirrors `memory/domain/search.py`'s own shape: a request is validated once,
by `filter_for`, which clamps what it can and refuses what it cannot — so
every `AuditStore` implementation (`sql_audit_store.py`, and the in-memory
one the unit suite uses) receives an already-sane `AuditFilter` instead of
repeating these bounds itself.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tiny_hermes.audit.domain.record import AuditRecord

#: A caller who names no limit gets this many rows.
DEFAULT_PAGE_SIZE = 50
#: The most one page ever holds, regardless of what a caller asks for —
#: clamped rather than refused, the same distinction
#: `memory/domain/search.py::request_for` draws for its own `limit`.
MAX_PAGE_SIZE = 200


class InvalidAuditFilter(Exception):
    """A request this platform will not run, and why."""


@dataclass(frozen=True)
class AuditFilter:
    action: str | None = None
    resource_type: str | None = None
    actor_id: UUID | None = None
    #: Both ends of the window are inclusive and optional independently —
    #: "since" with no "until" is "from here to now", not a mistake.
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


def filter_for(
    *,
    action: str | None = None,
    resource_type: str | None = None,
    actor_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> AuditFilter:
    if since is not None and until is not None and since > until:
        raise InvalidAuditFilter("the time window's start is after its end")
    if offset < 0:
        raise InvalidAuditFilter("offset cannot be negative")
    asked = DEFAULT_PAGE_SIZE if limit is None else limit
    if asked < 1:
        raise InvalidAuditFilter("a page holds at least one row")
    return AuditFilter(
        action=action,
        resource_type=resource_type,
        actor_id=actor_id,
        since=since,
        until=until,
        limit=min(asked, MAX_PAGE_SIZE),
        offset=offset,
    )


@dataclass(frozen=True)
class AuditPage:
    items: tuple[AuditRecord, ...]
    #: Whether a later offset would return more rows. Computed by asking a
    #: store for one row past `limit`, the same "fetch limit+1" trick every
    #: implementation of this protocol uses, so a caller never needs a
    #: separate `COUNT(*)`.
    has_more: bool
