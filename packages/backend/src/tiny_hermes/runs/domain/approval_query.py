"""What one page of the approvals queue may ask for.

§26's 治理审批队列. The queue existed before this module, but only as "every
pending row, oldest first" — which is the working list and not the queue. A
decision carries `decided_by`, `decided_at` and `decision_reason`, all three
written faithfully since §16.3 shipped and none of them readable by anyone
afterwards: the moment an administrator answered, the record of who answered
and why left the product. §26 also asks the team to evaluate 审批负担 after
M2, which is a question about decided rows.

Shaped after `audit/domain/query.py::filter_for` deliberately — validated
once here, so `sql_approval_store.py` receives an already-sane filter rather
than repeating these bounds.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tiny_hermes.runs.domain.approval import Approval, ApprovalStatus, ApprovalType

#: A caller who names no limit gets this many rows.
DEFAULT_PAGE_SIZE = 50
#: The most one page ever holds, clamped rather than refused.
MAX_PAGE_SIZE = 200


class InvalidApprovalFilter(Exception):
    """A request this platform will not run, and why."""


class QueueOrder(StrEnum):
    """Which end of the queue a reader means.

    Two orders because they answer two questions. Work is taken oldest first
    — that is what a queue is. History is read newest first, because somebody
    asking "what did we decide?" means recently, and offering only the M2
    ordering would make the newest decision the last row of the last page.
    """

    OLDEST_FIRST = "oldest_first"
    NEWEST_FIRST = "newest_first"


@dataclass(frozen=True)
class ApprovalFilter:
    #: Which statuses to include. Empty means every status — spelled as its
    #: own value rather than as an omission, so a caller that meant "the
    #: working queue" and forgot to say so does not silently receive
    #: months of decided rows in a list titled "waiting for a decision".
    statuses: tuple[ApprovalStatus, ...] = (ApprovalStatus.PENDING,)
    approval_type: ApprovalType | None = None
    tool: str | None = None
    #: Who answered. The point of history: "what did this administrator
    #: decide" is not answerable from any other column.
    decided_by: UUID | None = None
    #: Both ends inclusive and independent, over `created_at` — when the
    #: platform asked, not when a person answered. A window over the
    #: decision would hide the requests nobody has answered yet, which are
    #: exactly the ones a queue is about.
    since: datetime | None = None
    until: datetime | None = None
    order: QueueOrder = QueueOrder.OLDEST_FIRST
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


@dataclass(frozen=True)
class ApprovalPage:
    items: tuple[Approval, ...]
    #: Whether a later offset would return more. Computed by fetching one row
    #: past `limit`, the same trick `AuditPage` uses, so no caller needs a
    #: separate `COUNT(*)`.
    has_more: bool


def filter_for(
    *,
    statuses: tuple[ApprovalStatus, ...] | None = None,
    approval_type: ApprovalType | None = None,
    tool: str | None = None,
    decided_by: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    order: QueueOrder | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> ApprovalFilter:
    if since is not None and until is not None and since > until:
        raise InvalidApprovalFilter("the time window's start is after its end")
    if offset < 0:
        raise InvalidApprovalFilter("offset cannot be negative")
    asked = DEFAULT_PAGE_SIZE if limit is None else limit
    if asked < 1:
        raise InvalidApprovalFilter("a page holds at least one row")
    if tool is not None and tool.strip() == "":
        # Not silently dropped: a blank filter that behaves like no filter
        # returns the whole queue to somebody who believes they narrowed it.
        raise InvalidApprovalFilter("a tool filter cannot be blank")
    return ApprovalFilter(
        statuses=(ApprovalStatus.PENDING,) if statuses is None else statuses,
        approval_type=approval_type,
        tool=tool,
        decided_by=decided_by,
        since=since,
        until=until,
        order=QueueOrder.OLDEST_FIRST if order is None else order,
        limit=min(asked, MAX_PAGE_SIZE),
        offset=offset,
    )
