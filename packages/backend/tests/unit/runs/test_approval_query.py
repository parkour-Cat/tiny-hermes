"""What the queue's filter accepts, refuses and quietly changes.

The three that matter are not the clamp. They are the defaults: what a
caller gets when they say nothing, and what "everything" has to be spelled
as. A queue whose default answer is "every row ever decided" is a different
product from one whose default is "waiting for a person", and the difference
is invisible at the call site.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.approval import ApprovalStatus, ApprovalType
from tiny_hermes.runs.domain.approval_query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidApprovalFilter,
    QueueOrder,
    filter_for,
)


def test_saying_nothing_asks_for_the_working_queue_not_the_archive() -> None:
    # The route this backs is titled "waiting for a decision". If the default
    # were every status, that page would fill with months of answered rows
    # and the person working it would have to filter to see their own work.
    built = filter_for()

    assert built.statuses == (ApprovalStatus.PENDING,)
    assert built.limit == DEFAULT_PAGE_SIZE
    assert built.offset == 0
    assert built.order is QueueOrder.OLDEST_FIRST


def test_every_status_is_something_a_caller_has_to_say() -> None:
    """Empty means all — but only when written, never by forgetting."""
    built = filter_for(statuses=())

    assert built.statuses == ()


def test_history_may_be_read_newest_first() -> None:
    # A queue is worked oldest first; a history read oldest first buries the
    # most recent decision at the end of the last page.
    built = filter_for(statuses=(ApprovalStatus.APPROVED,), order=QueueOrder.NEWEST_FIRST)

    assert built.order is QueueOrder.NEWEST_FIRST


def test_a_page_above_the_ceiling_is_clamped_rather_than_refused() -> None:
    assert filter_for(limit=MAX_PAGE_SIZE * 10).limit == MAX_PAGE_SIZE


def test_a_backwards_window_is_refused_rather_than_returning_nothing() -> None:
    # Nothing found and nothing possible look the same to a reader, and this
    # one is a typo in the dates rather than a quiet workspace.
    now = datetime.now(UTC)
    with pytest.raises(InvalidApprovalFilter):
        filter_for(since=now, until=now - timedelta(days=1))


def test_a_blank_tool_filter_is_refused_rather_than_ignored() -> None:
    # An ignored blank returns the whole queue to somebody who believes they
    # narrowed it — the worst of the two failures, because it looks like an
    # answer.
    with pytest.raises(InvalidApprovalFilter):
        filter_for(tool="   ")


def test_a_page_of_no_rows_is_refused() -> None:
    with pytest.raises(InvalidApprovalFilter):
        filter_for(limit=0)


def test_a_negative_offset_is_refused() -> None:
    with pytest.raises(InvalidApprovalFilter):
        filter_for(offset=-1)


def test_the_named_filters_survive_construction() -> None:
    # Cheap, and it catches the one-word slip where a builder drops a field
    # on the floor: the caller narrowed, the store never heard about it, and
    # the result looks like a filter that matched a lot.
    decider = uuid4()
    built = filter_for(
        statuses=(ApprovalStatus.REJECTED,),
        approval_type=ApprovalType.GOVERNANCE_APPROVAL,
        tool="http.orders.createOrder",
        decided_by=decider,
    )

    assert built.statuses == (ApprovalStatus.REJECTED,)
    assert built.approval_type is ApprovalType.GOVERNANCE_APPROVAL
    assert built.tool == "http.orders.createOrder"
    assert built.decided_by == decider
