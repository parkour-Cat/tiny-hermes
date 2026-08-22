"""Feishu's own envelope, read into a `ChannelEvent`.

Shared by both transports on purpose — see `events.ChannelEvent`.
"""

import json
from typing import Any, cast

from tiny_hermes.channels.domain._json import object_at, string_at
from tiny_hermes.channels.domain.events import ChannelEvent, MalformedChannelEvent

CHANNEL = "feishu"


def _text_of(message: dict[str, Any]) -> str:
    """The message body, for the one message type §19.2's first version
    promises. `content` is itself a JSON *string* rather than an object —
    a quirk of the wire format, not of this reader."""
    raw = string_at(message, "content")
    if raw is None:
        raise MalformedChannelEvent("message content is not a string")
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedChannelEvent("message content is not JSON") from error
    if not isinstance(parsed, dict):
        raise MalformedChannelEvent("message content is not an object")
    text = string_at(cast(dict[str, Any], parsed), "text")
    if text is None:
        raise MalformedChannelEvent("message carries no text")
    return text


def event_from_envelope(payload: dict[str, Any]) -> ChannelEvent:
    """Both schema versions, because a tenant chooses which one it sends.

    v2 puts the id in `header.event_id`; v1 put it at the top level as
    `uuid`. Refusing v1 outright would be a deployment-time surprise rather
    than a platform decision, and the id is the only part of it this needs.
    """
    header = object_at(payload, "header")
    event_id = string_at(header, "event_id") if header else string_at(payload, "uuid")
    if event_id is None:
        raise MalformedChannelEvent("no event id in either schema version")

    event = object_at(payload, "event")
    if event is None:
        raise MalformedChannelEvent("no event body")

    sender = object_at(event, "sender")
    sender_id = object_at(sender, "sender_id") if sender else None
    open_id = string_at(sender_id, "open_id") if sender_id else None
    if open_id is None:
        raise MalformedChannelEvent("no sender open_id")

    message = object_at(event, "message")
    if message is None:
        raise MalformedChannelEvent("no message")

    return ChannelEvent(
        channel=CHANNEL,
        channel_event_id=event_id,
        external_user_id=open_id,
        text=_text_of(message),
    )
