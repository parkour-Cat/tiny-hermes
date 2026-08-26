"""Feishu's own envelope, read into a `ChannelEvent`.

Shared by both transports on purpose — see `events.ChannelEvent`.
"""

import json
import logging
from typing import Any, cast

from tiny_hermes.channels.domain._json import object_at, string_at
from tiny_hermes.channels.domain.events import (
    ChannelEvent,
    ChannelImage,
    MalformedChannelEvent,
)

logger = logging.getLogger(__name__)

CHANNEL = "feishu"

#: The message types §19.2's first version reads. A type outside this set is
#: refused *answerably* — see `UnsupportedMessageType`. Absent is read as
#: text: Feishu's v1 schema omitted the field on text messages.
_READABLE = frozenset({"text", "", "image", "post"})


class UnsupportedMessageType(MalformedChannelEvent):
    """A message this build cannot read, from somebody who can be told so.

    Subclassed rather than parallel so a transport that has not learned
    about it keeps refusing the delivery, instead of letting an unreadable
    message through as if it were text.

    It carries the sender and the event id because that is the whole point:
    a photo is not a broken envelope, it is a person who sent something and
    is now looking at their phone. §19.2 forbids swallowing that quietly,
    and the branch that used to handle it wrote a log line — which is not
    read by the only person who needed telling.
    """

    def __init__(
        self, kind: str, *, channel_event_id: str, external_user_id: str
    ) -> None:
        super().__init__(f"message type {kind!r} is not supported")
        self.kind = kind
        self.channel_event_id = channel_event_id
        self.external_user_id = external_user_id


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

    # Checked before the content is read, so an unreadable type is refused
    # for being that type rather than for "carrying no text" — the second is
    # true of a voice note but says nothing a person could act on, and it is
    # also what a genuinely broken text message looks like.
    kind = string_at(message, "message_type") or ""
    if kind not in _READABLE:
        raise UnsupportedMessageType(
            kind, channel_event_id=event_id, external_user_id=open_id
        )

    if kind == "post":
        # Logged because a picture's `image_key` and the message it can be
        # downloaded from are not always the same message. A quoted reply
        # carries the original's image inline while being its own message,
        # and Feishu answers `234003 File not in msg` for the pairing — a
        # true error whose cause is invisible without knowing the parentage.
        logger.info(
            "feishu post: message=%s parent=%s root=%s",
            string_at(message, "message_id"),
            string_at(message, "parent_id"),
            string_at(message, "root_id"),
        )
        said, pictures = _post_of(message)
        if not said and not pictures:
            # Nothing to act on. A Run built from this hands the Agent an
            # empty turn and charges somebody for the round.
            raise MalformedChannelEvent("rich text message carried nothing readable")
        return ChannelEvent(
            channel=CHANNEL,
            channel_event_id=event_id,
            external_user_id=open_id,
            text=said,
            images=pictures,
        )

    if kind == "image":
        return ChannelEvent(
            channel=CHANNEL,
            channel_event_id=event_id,
            external_user_id=open_id,
            # Feishu sends a photo as its own message with no caption field.
            # Empty rather than invented: whatever the Run is asked to do
            # with the picture is the caller's wording, not the parser's.
            text="",
            images=(_image_of(message),),
        )

    return ChannelEvent(
        channel=CHANNEL,
        channel_event_id=event_id,
        external_user_id=open_id,
        text=_text_of(message),
    )


def _post_of(message: dict[str, Any]) -> tuple[str, tuple[ChannelImage, ...]]:
    """Everything a rich-text message says, and every picture in it.

    The content is language-keyed — `{"zh_cn": {...}}` — and the event
    carries whichever locale the sender's client used. Reading the first
    key rather than looking for `zh_cn` is why an English speaker's message
    does not vanish.

    Paragraphs are joined with a newline and runs inside one are not:
    Feishu splits a styled line into several runs, and they are one line to
    the person who typed them.
    """
    raw = string_at(message, "content")
    if raw is None:
        raise MalformedChannelEvent("rich text content is not a string")
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedChannelEvent("rich text content is not JSON") from error
    if not isinstance(parsed, dict):
        raise MalformedChannelEvent("rich text content is not an object")
    envelope = cast(dict[str, Any], parsed)
    # Two shapes, and the documented one is not the one that arrives.
    #
    # Feishu documents the **send** format, keyed by locale:
    # `{"zh_cn": {"title": ..., "content": [...]}}`. What comes back in an
    # `im.message.receive_v1` event is already resolved to one language and
    # carries `content` at the top level. Ten unit tests written from the
    # documentation passed while every real message failed — the tests and
    # this parser agreed with each other and both disagreed with the
    # service, which is a shape this repository has produced before.
    #
    # Both are accepted. An SDK may hand over the documented one verbatim,
    # and nothing is gained by refusing a structure this can read.
    if isinstance(envelope.get("content"), list):
        document = envelope
    else:
        body: Any = next(iter(envelope.values()), None)
        if not isinstance(body, dict):
            raise MalformedChannelEvent("rich text content carries no readable body")
        document = cast(dict[str, Any], body)

    lines: list[str] = []
    title = string_at(document, "title")
    if title:
        # A person who filled it in meant it to be read.
        lines.append(title)
    pictures: list[ChannelImage] = []
    paragraphs: Any = document.get("content")
    for entry in cast(list[Any], paragraphs) if isinstance(paragraphs, list) else []:
        if not isinstance(entry, list):
            continue
        runs: list[str] = []
        for item in cast(list[Any], entry):
            if not isinstance(item, dict):
                continue
            element = cast(dict[str, Any], item)
            tag = string_at(element, "tag")
            if tag in ("text", "a"):
                # A link's visible words are part of the sentence; dropping
                # them removes a piece of what was said.
                runs.append(string_at(element, "text") or "")
            elif tag == "img":
                key = string_at(element, "image_key")
                if key:
                    pictures.append(
                        ChannelImage(
                            message_id=_message_id_of(message), file_key=key
                        )
                    )
            # Anything else — a divider, an emoji, a code block — is skipped.
            # It is not content this build renders, and it is also not a
            # reason to refuse a message whose words came through fine.
        joined = "".join(runs)
        if joined:
            lines.append(joined)
    return "\n".join(lines), tuple(pictures)


def _message_id_of(message: dict[str, Any]) -> str:
    message_id = string_at(message, "message_id")
    if message_id is None:
        raise MalformedChannelEvent("message carries no message id")
    return message_id


def _image_of(message: dict[str, Any]) -> ChannelImage:
    """Both ids a download needs, or a refusal.

    Half an address is not an address: refusing here keeps a fetch that
    could never succeed from being attempted, and keeps the sender's
    refusal accurate rather than a timeout.
    """
    message_id = _message_id_of(message)
    raw = string_at(message, "content")
    if raw is None:
        raise MalformedChannelEvent("image message content is not a string")
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedChannelEvent("image content is not JSON") from error
    if not isinstance(parsed, dict):
        raise MalformedChannelEvent("image content is not an object")
    file_key = string_at(cast(dict[str, Any], parsed), "image_key")
    if file_key is None:
        raise MalformedChannelEvent("image message carries no image key")
    return ChannelImage(message_id=message_id, file_key=file_key)
