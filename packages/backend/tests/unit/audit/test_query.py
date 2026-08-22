"""What one page of the audit trail may ask for — mirrors
`memory/domain/search.py`'s own `request_for`: validated once, by a function
that clamps rather than trusts, so no store repeats these bounds itself.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tiny_hermes.audit.domain.query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidAuditFilter,
    filter_for,
)


def test_defaults_are_a_bounded_page_newest_nothing_excluded() -> None:
    built = filter_for()

    assert built.limit == DEFAULT_PAGE_SIZE
    assert built.offset == 0
    assert built.action is None
    assert built.resource_type is None
    assert built.actor_id is None
    assert built.since is None
    assert built.until is None


def test_limit_is_clamped_not_refused() -> None:
    """A caller asking for more than the ceiling is asking for more than a
    page holds, not making a mistake — the same distinction
    `memory/domain/search.py::request_for` draws between clamping and
    refusing."""
    built = filter_for(limit=MAX_PAGE_SIZE + 500)

    assert built.limit == MAX_PAGE_SIZE


def test_a_reversed_time_window_is_refused() -> None:
    now = datetime.now(UTC)
    with pytest.raises(InvalidAuditFilter):
        filter_for(since=now, until=now - timedelta(days=1))


def test_negative_offset_is_refused() -> None:
    with pytest.raises(InvalidAuditFilter):
        filter_for(offset=-1)


def test_zero_or_negative_limit_is_refused() -> None:
    with pytest.raises(InvalidAuditFilter):
        filter_for(limit=0)


def test_filters_pass_through_unchanged() -> None:
    actor = uuid4()
    built = filter_for(action="run.created", resource_type="run", actor_id=actor)

    assert built.action == "run.created"
    assert built.resource_type == "run"
    assert built.actor_id == actor
