"""§19.2's status card: what a person sees when their message is queued.

§497 lets the platform *save* a pending Run behind a blocked head. What it
forbids is doing that silently. On a console the queue is visible; in a
chat window nothing is, so a message that merely queues looks exactly like
a message that was lost — and the person sends it again, which queues
another one.

`BlockedNotice` has carried every fact §497 names since the channel landed.
It reached the webhook's HTTP response body and stopped there — a body
Feishu's server discards the moment it reads the 200. Nobody ever saw it.
This is the rendering that gets it in front of the person who typed.
"""

import json
from uuid import UUID

from tiny_hermes.channels.domain.blocked import BlockedNotice
from tiny_hermes.channels.infrastructure.feishu_card import blocked_card

BLOCKER = UUID("11111111-2222-4333-8444-555555555555")


def _notice(**overrides: object) -> BlockedNotice:
    fields: dict[str, object] = {
        "blocked_by_run_id": BLOCKER,
        "head_status": "waiting_approval",
        "pause_reason": None,
        "wait_kind": None,
        "position": 2,
        "available_actions": (),
    }
    fields.update(overrides)
    return BlockedNotice(**fields)  # pyright: ignore[reportArgumentType]


def _text_of(card: dict[str, object]) -> str:
    """Every piece of text in the card, flattened — the test asks what a
    person can read, not which element it landed in."""
    return json.dumps(card, ensure_ascii=False)


def test_the_card_says_the_message_was_queued_rather_than_lost() -> None:
    rendered = _text_of(blocked_card(_notice()))

    assert "排队" in rendered or "队列" in rendered


def test_the_card_gives_the_position_in_the_queue() -> None:
    """§497 names it explicitly. "Somewhere behind something" is what the
    person already assumes; the number is the part that says the platform
    knows what it is doing."""
    rendered = _text_of(blocked_card(_notice(position=3)))

    assert "3" in rendered


def test_the_card_says_why_the_head_is_blocked() -> None:
    waiting = _text_of(blocked_card(_notice(head_status="waiting_approval")))
    paused = _text_of(blocked_card(_notice(head_status="paused", pause_reason="limit")))

    # Not the raw enum: `waiting_approval` is a state name this platform uses
    # internally, and a person reading a chat message is not owed the
    # vocabulary of a state machine.
    assert "waiting_approval" not in waiting
    assert waiting != paused


def test_no_available_action_says_somebody_else_must_act() -> None:
    """The most important case, and the easy one to render as nothing.

    A Feishu user is an `EndUser`, not a workspace member, so `can_control`
    is false and this list is usually empty. An empty list is not missing
    information — it means the person must wait for somebody else, and a
    card that showed an empty section would leave them with a queue notice
    and no idea whether they were expected to do something about it.
    """
    rendered = _text_of(blocked_card(_notice(available_actions=())))

    assert "等" in rendered


def test_the_actions_a_subject_does_have_are_named_in_words() -> None:
    rendered = _text_of(
        blocked_card(_notice(available_actions=("resume", "cancel")))
    )

    assert "继续" in rendered
    assert "取消" in rendered
    # The slugs are the platform's names for these, not the reader's.
    assert "resume" not in rendered


def test_an_unknown_action_is_shown_rather_than_dropped() -> None:
    """A slug this build has no wording for is still an action the caller
    may take. Dropping it would quietly shorten the list §497 requires;
    showing the slug is worse reading and better information."""
    rendered = _text_of(blocked_card(_notice(available_actions=("teleport",))))

    assert "teleport" in rendered


def test_a_console_link_is_offered_when_the_deployment_has_one() -> None:
    """§19.2 allows `必要的审批卡片或跳转管理页面` — a link is a compliant
    entry point, and it is the one this milestone builds."""
    card = blocked_card(_notice(), console_url="https://console.example.com")

    assert "https://console.example.com" in _text_of(card)


def test_no_link_is_invented_when_the_deployment_has_no_console_url() -> None:
    """A private deployment's console address is known only to whoever
    deployed it. A guessed URL in a card is a dead link in front of a user,
    which is worse than no button."""
    card = blocked_card(_notice(), console_url=None)

    assert "http" not in _text_of(card)


def test_the_card_is_the_shape_feishu_takes() -> None:
    card = blocked_card(_notice())

    assert "elements" in card
    assert isinstance(card["elements"], list)
    # Serializable as-is: the sender puts this through `json.dumps`, and a
    # value that could not survive that would fail at the vendor rather than
    # here.
    json.dumps(card)
