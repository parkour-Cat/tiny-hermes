"""A message this platform cannot read is still a person waiting for an answer.

§19.2 forbids swallowing a message quietly. The blocked-head path stopped
doing that; this one never did. A photo, a voice note or a file produced a
`MalformedChannelEvent`, a 200, and a log line — and the log line is not
read by the person who sent it. The comment on that branch said `never
silently`, which was true about the platform's records and false about the
only reader who mattered.

The distinction this module draws: an envelope that is genuinely broken has
nobody to answer, because there is no sender in it. One carrying a message
type this build cannot read has a sender, an event id, and a person looking
at their phone.
"""

import json
from typing import Any

import pytest
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.feishu import (
    UnsupportedMessageType,
    event_from_envelope,
)


def _envelope(message: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body = {
        "header": {"event_id": "om_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": message,
        },
    }
    body.update(overrides)
    return body


def test_a_text_message_is_read_as_before() -> None:
    event = event_from_envelope(
        _envelope({"message_type": "text", "content": json.dumps({"text": "在吗"})})
    )

    assert event.text == "在吗"


def test_an_unreadable_type_is_refused_in_a_way_that_can_be_answered() -> None:
    """A voice note, which this build still cannot read.

    `UnsupportedMessageType` carries the sender and the event id precisely
    so the transport can claim the delivery and reply. A plain
    `MalformedChannelEvent` could not be answered — there would be nobody
    named to answer.

    This used to be an image, which was the first thing a real person sent.
    Images are read now; the property this test protects belongs to whatever
    is still unreadable.
    """
    with pytest.raises(UnsupportedMessageType) as refused:
        event_from_envelope(
            _envelope(
                {"message_type": "audio", "content": json.dumps({"file_key": "f_1"})}
            )
        )

    assert refused.value.kind == "audio"
    assert refused.value.channel_event_id == "om_1"
    assert refused.value.external_user_id == "ou_zhang"


@pytest.mark.parametrize("kind", ["audio", "file", "post", "sticker", "media"])
def test_every_other_message_type_is_refused_the_same_way(kind: str) -> None:
    with pytest.raises(UnsupportedMessageType):
        event_from_envelope(_envelope({"message_type": kind, "content": "{}"}))


def test_an_unsupported_type_is_still_a_malformed_event() -> None:
    """Subclassed rather than parallel, so a transport that has not learned
    about this yet keeps refusing the delivery instead of letting an
    unreadable message through as if it were text."""
    assert issubclass(UnsupportedMessageType, MalformedChannelEvent)


def test_an_envelope_with_no_sender_has_nobody_to_answer() -> None:
    """Genuinely broken, and it stays a plain refusal. Inventing a
    recipient would be worse than silence: the reply would go to whoever
    the platform guessed."""
    broken = {
        "header": {"event_id": "om_1"},
        "event": {"message": {"message_type": "image", "content": "{}"}},
    }

    with pytest.raises(MalformedChannelEvent) as refused:
        event_from_envelope(broken)

    assert not isinstance(refused.value, UnsupportedMessageType)


def test_a_text_message_with_no_text_is_broken_rather_than_unsupported() -> None:
    """`message_type: text` and no `text` field is not a type this build
    cannot read — it is a text message that arrived wrong. Answering "I
    only handle text" to that would be a confusing lie."""
    with pytest.raises(MalformedChannelEvent) as refused:
        event_from_envelope(_envelope({"message_type": "text", "content": "{}"}))

    assert not isinstance(refused.value, UnsupportedMessageType)


def test_a_message_with_no_type_at_all_is_treated_as_text() -> None:
    """Feishu's v1 schema omitted `message_type` on text messages, and the
    existing tests in this repository send envelopes without it. Reading a
    missing type as text keeps those working rather than turning every one
    of them into a refusal."""
    event = event_from_envelope(
        _envelope({"content": json.dumps({"text": "老格式"})})
    )

    assert event.text == "老格式"
