"""§497 on a surface that shows no queue.

A console user can look at a queue; a chat user cannot. So a message that
is merely queued looks exactly like a message that was lost, and the person
sends it again — which is why "不得静默排队" is stricter here than anywhere
else in the product.
"""

from typing import Any
from uuid import uuid4

from tiny_hermes.channels.domain.blocked import notice_from_document

BLOCKING = uuid4()


def _document(**queue: Any) -> dict[str, Any]:
    return {"id": str(uuid4()), "queue": queue}


def test_an_accepted_run_produces_no_notice() -> None:
    """The ordinary case has to stay quiet, or the notice becomes noise the
    reader learns to skip — and then it is not there when it matters."""
    assert notice_from_document(_document(position=0, status="queued")) is None


def test_a_blocked_run_carries_every_fact_the_rule_names() -> None:
    """Named one by one rather than asserted as a blob: §497 lists reason,
    blocking Run, queue position and available actions, and a notice missing
    any one of them leaves the person unable to act on it."""
    notice = notice_from_document(
        _document(
            position=2,
            status="session_blocked",
            blocked_by_run_id=str(BLOCKING),
            head_status="waiting_approval",
            head_reason={"pause_reason": None, "wait_kind": "user_confirmation"},
            available_actions=["approve", "reject", "cancel"],
        )
    )

    assert notice is not None
    assert notice.blocked_by_run_id == BLOCKING
    assert notice.head_status == "waiting_approval"
    assert notice.wait_kind == "user_confirmation"
    assert notice.position == 2
    assert notice.available_actions == ("approve", "reject", "cancel")


def test_no_available_actions_is_a_fact_rather_than_a_gap() -> None:
    """An empty list says "you are waiting on somebody else", which is worth
    telling a person. Silently treating it as unknown would leave them
    staring at a stalled chat with nothing to do and no reason given."""
    notice = notice_from_document(
        _document(
            position=1,
            status="session_blocked",
            blocked_by_run_id=str(BLOCKING),
            head_status="waiting_external",
            head_reason={"pause_reason": None, "wait_kind": "child_runs"},
            available_actions=[],
        )
    )

    assert notice is not None
    assert notice.available_actions == ()
    assert notice.wait_kind == "child_runs"


def test_a_paused_head_reports_its_pause_reason_not_just_paused() -> None:
    """"Paused" and "paused because it hit its budget" are different things
    to be told, which is why the reason is carried beside the state rather
    than folded into it."""
    notice = notice_from_document(
        _document(
            position=1,
            status="session_blocked",
            blocked_by_run_id=str(BLOCKING),
            head_status="paused",
            head_reason={"pause_reason": "budget_exhausted", "wait_kind": None},
            available_actions=["resume", "cancel"],
        )
    )

    assert notice is not None
    assert notice.head_status == "paused"
    assert notice.pause_reason == "budget_exhausted"


def test_a_document_with_no_queue_at_all_is_not_a_crash() -> None:
    """The channel reads a document the platform owns. A shape it does not
    recognize must mean "nothing to say", not an exception that turns a
    delivered message into a 500 and a Feishu retry."""
    assert notice_from_document({"id": str(uuid4())}) is None
