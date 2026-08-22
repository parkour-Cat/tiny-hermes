"""Feishu's own envelope, read into a `ChannelEvent`.

Shared by both transports on purpose — see `events.ChannelEvent`.
"""

import json
from typing import Any, cast

from tiny_hermes.channels.domain.events import ChannelEvent, MalformedChannelEvent

CHANNEL = "feishu"


def _object_at(container: dict[str, Any], key: str) -> dict[str, Any] | None:
    """`isinstance` narrows to `dict[Unknown, Unknown]`, so the cast is what
    states the shape JSON actually guarantees: object keys are strings."""
    value: object = container.get(key)
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _string_at(container: dict[str, Any], key: str) -> str | None:
    value: object = container.get(key)
    return value if isinstance(value, str) and value != "" else None


def _text_of(message: dict[str, Any]) -> str:
    """The message body, for the one message type §19.2's first version
    promises. `content` is itself a JSON *string* rather than an object —
    a quirk of the wire format, not of this reader."""
    raw = _string_at(message, "content")
    if raw is None:
        raise MalformedChannelEvent("message content is not a string")
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedChannelEvent("message content is not JSON") from error
    if not isinstance(parsed, dict):
        raise MalformedChannelEvent("message content is not an object")
    text = _string_at(cast(dict[str, Any], parsed), "text")
    if text is None:
        raise MalformedChannelEvent("message carries no text")
    return text


def event_from_envelope(payload: dict[str, Any]) -> ChannelEvent:
    """Both schema versions, because a tenant chooses which one it sends.

    v2 puts the id in `header.event_id`; v1 put it at the top level as
    `uuid`. Refusing v1 outright would be a deployment-time surprise rather
    than a platform decision, and the id is the only part of it this needs.
    """
    header = _object_at(payload, "header")
    event_id = _string_at(header, "event_id") if header else _string_at(payload, "uuid")
    if event_id is None:
        raise MalformedChannelEvent("no event id in either schema version")

    event = _object_at(payload, "event")
    if event is None:
        raise MalformedChannelEvent("no event body")

    sender = _object_at(event, "sender")
    sender_id = _object_at(sender, "sender_id") if sender else None
    open_id = _string_at(sender_id, "open_id") if sender_id else None
    if open_id is None:
        raise MalformedChannelEvent("no sender open_id")

    message = _object_at(event, "message")
    if message is None:
        raise MalformedChannelEvent("no message")

    return ChannelEvent(
        channel=CHANNEL,
        channel_event_id=event_id,
        external_user_id=open_id,
        text=_text_of(message),
    )
