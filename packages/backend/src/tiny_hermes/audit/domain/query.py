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
#: The most one export ever holds. Deliberately not `MAX_PAGE_SIZE`: an
#: export is the whole of what a reader may see under their filters, not a
#: page of it, and running it through `filter_for` — which clamps to
#: `MAX_PAGE_SIZE` — is how the first version of the export route came to
#: return 200 rows out of any number, silently. A file that stops early
#: still opens, still has the right columns, and every row in it is true.
MAX_EXPORT_ROWS = 50_000


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
    return _filter_within(
        MAX_PAGE_SIZE,
        action=action,
        resource_type=resource_type,
        actor_id=actor_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


def export_filter_for(
    *,
    action: str | None = None,
    resource_type: str | None = None,
    actor_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> AuditFilter:
    """One export's worth of the same request `filter_for` validates.

    Same window and same refusals — only the ceiling differs, so the two
    doors onto `audit_events` cannot come to disagree about what a filter
    means. No `offset`: an export is not paged.
    """
    return _filter_within(
        MAX_EXPORT_ROWS,
        action=action,
        resource_type=resource_type,
        actor_id=actor_id,
        since=since,
        until=until,
        limit=MAX_EXPORT_ROWS,
        offset=0,
    )


def _filter_within(
    ceiling: int,
    *,
    action: str | None,
    resource_type: str | None,
    actor_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
    limit: int | None,
    offset: int,
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
        limit=min(asked, ceiling),
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
