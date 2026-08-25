"""How an image in a transcript names the thing that can fetch it.

The reference lives in `session_messages`; the bytes are fetched when a
round is built. Nothing sits between the two — no lookup table, no row
somewhere else — so the string has to carry everything a fetch needs, and a
Run replayed weeks later has to resolve the same one.

`feishu:{message_id}:{file_key}`, because Feishu's download takes both:
`GET /im/v1/messages/{message_id}/resources/{file_key}`.
"""

from dataclasses import dataclass

PREFIX = "feishu"


class MalformedImageReference(Exception):
    """A reference that cannot be resolved.

    Raised rather than skipped. The alternative is a request that reaches
    the model carrying a question about a picture and no picture, which gets
    answered confidently and with no sign to the reader that anything was
    missing.
    """


@dataclass(frozen=True)
class FeishuImageReference:
    message_id: str
    file_key: str


def feishu_reference(*, message_id: str, file_key: str) -> str:
    return f"{PREFIX}:{message_id}:{file_key}"


def parse_reference(reference: str) -> FeishuImageReference:
    """Read a reference, or refuse.

    Split from the left exactly twice: the file key is the vendor's and may
    contain colons, and a naive `split(":")` would truncate it into
    something that fetches nothing.
    """
    parts = reference.split(":", 2)
    if len(parts) != 3:
        raise MalformedImageReference(f"not a resolvable reference: {reference!r}")
    surface, message_id, file_key = parts
    if surface != PREFIX:
        raise MalformedImageReference(f"unknown surface in reference: {surface!r}")
    if not message_id or not file_key:
        raise MalformedImageReference(f"incomplete reference: {reference!r}")
    return FeishuImageReference(message_id=message_id, file_key=file_key)


__all__ = [
    "FeishuImageReference",
    "MalformedImageReference",
    "feishu_reference",
    "parse_reference",
]
