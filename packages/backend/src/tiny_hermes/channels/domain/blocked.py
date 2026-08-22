"""What a channel must say back when the Session is not free to run.

§497 permits a new pending Run to be *saved* while the head is `paused` or
`waiting_*`. What it forbids is doing that **silently**: the caller has to
be told, immediately, why it is blocked, which Run is blocking it, where in
the queue it landed, and which actions this subject can take. §19.2 repeats
the requirement for Feishu specifically — a status card, not silence.

The distinction matters more on a chat surface than anywhere else. A console
shows a queue the user can look at; a chat shows nothing, so a message that
merely queues looks exactly like a message that was lost, and the person
sends it again.

This is the *notice*, not its rendering. Turning it into Feishu's
interactive-card JSON belongs with the adapter that talks to Feishu, and
that rendering cannot be verified against the vendor from here — the same
limit that keeps the WebSocket transport out of this milestone. Everything
in this module is checkable without a tenant, and that is deliberate: the
facts §497 demands are the part worth pinning.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from tiny_hermes.channels.domain._json import (
    int_at,
    object_at,
    string_at,
    strings_at,
)


@dataclass(frozen=True)
class BlockedNotice:
    """Every fact §497 names, and nothing invented to fill a gap."""

    blocked_by_run_id: UUID | None
    #: `paused` / `waiting_approval` / `waiting_external` — the head's state,
    #: which is *why* this message is queued.
    head_status: str | None
    #: The specific reason under that state, when the platform recorded one.
    #: Kept separate from `head_status` because "waiting" and "waiting for an
    #: approval nobody has answered" are different things to tell a person.
    pause_reason: str | None
    wait_kind: str | None
    position: int
    #: What this subject may do about it. Empty is meaningful and is not the
    #: same as unknown: it says the person must wait for somebody else, which
    #: is worth telling them rather than offering nothing and no explanation.
    available_actions: tuple[str, ...]


def notice_from_document(document: dict[str, Any]) -> BlockedNotice | None:
    """`None` when the Run was accepted normally.

    Reads the queue the platform already publishes rather than re-deriving
    anything: `RunSnapshot._queue_document` is the one place that decides
    what a blocked queue looks like, and a channel computing its own answer
    would drift from the console's the first time that shape changed.
    """
    queue = object_at(document, "queue")
    if queue is None or string_at(queue, "status") != "session_blocked":
        return None

    reason = object_at(queue, "head_reason") or {}
    blocking = string_at(queue, "blocked_by_run_id")

    return BlockedNotice(
        blocked_by_run_id=UUID(blocking) if blocking is not None else None,
        head_status=string_at(queue, "head_status"),
        pause_reason=string_at(reason, "pause_reason"),
        wait_kind=string_at(reason, "wait_kind"),
        position=int_at(queue, "position", 0),
        available_actions=strings_at(queue, "available_actions"),
    )
