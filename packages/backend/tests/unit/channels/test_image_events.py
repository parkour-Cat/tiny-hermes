"""A Feishu image message, read as something the platform can act on.

Until now this raised `UnsupportedMessageType` and the sender got a polite
refusal. That was honest — nothing downstream could carry an image — and it
stops being honest the moment the platform can.

What a Feishu image message gives us is not the picture: it is an
`image_key` and the id of the message it arrived in. Both are needed to
fetch it (`GET /im/v1/messages/{message_id}/resources/{file_key}`), so both
have to survive parsing. An event carrying only one of them is an event
that cannot be completed.
"""

import json
from typing import Any

import pytest
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.feishu import (
    UnsupportedMessageType,
    event_from_envelope,
)


def _envelope(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {"event_id": "om_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": message,
        },
    }


def _image(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_type": "image",
        "message_id": "om_msg_1",
        "content": json.dumps({"image_key": "img_v2_abc"}),
    }
    message.update(overrides)
    return message


def test_an_image_message_is_read_rather_than_refused() -> None:
    event = event_from_envelope(_envelope(_image()))

    assert event.external_user_id == "ou_zhang"
    assert len(event.images) == 1


def test_the_event_carries_both_ids_a_download_needs() -> None:
    """`GET /im/v1/messages/{message_id}/resources/{file_key}` takes both.
    An event with only the key names a file nothing can locate."""
    event = event_from_envelope(_envelope(_image()))

    picture = event.images[0]
    assert picture.message_id == "om_msg_1"
    assert picture.file_key == "img_v2_abc"


def test_an_image_message_has_no_text_of_its_own() -> None:
    """Feishu sends a photo as its own message, with no caption field. The
    Run still needs something to act on, so the caller supplies the wording
    — not the parser, which would be inventing content."""
    event = event_from_envelope(_envelope(_image()))

    assert event.text == ""


def test_an_image_message_missing_its_id_is_still_refused() -> None:
    """Half an address is not an address. Refusing here keeps a download
    that could never succeed from being attempted, and keeps the sender's
    refusal accurate rather than a timeout."""
    with pytest.raises(MalformedChannelEvent):
        event_from_envelope(_envelope(_image(message_id=None)))


def test_an_image_message_missing_its_key_is_still_refused() -> None:
    with pytest.raises(MalformedChannelEvent):
        event_from_envelope(_envelope(_image(content=json.dumps({}))))


def test_a_text_message_carries_no_images() -> None:
    event = event_from_envelope(
        _envelope({"message_type": "text", "content": json.dumps({"text": "在吗"})})
    )

    assert event.images == ()
    assert event.text == "在吗"


@pytest.mark.parametrize("kind", ["audio", "file", "sticker"])
def test_other_message_types_are_still_refused(kind: str) -> None:
    """Only images. A voice note needs transcription and a file needs a
    reader, and neither exists — saying so remains the honest answer."""
    with pytest.raises(UnsupportedMessageType):
        event_from_envelope(_envelope({"message_type": kind, "content": "{}"}))
