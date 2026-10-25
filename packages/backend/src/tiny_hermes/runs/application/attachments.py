"""Files somebody sent, and the sandbox inbox they are placed in (§1.27, v2.27)."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from tiny_hermes.runs.domain.models import FileBlock, StoredMessage


class FileSource(Protocol):
    """Whatever turns one attachment reference into its bytes.

    `ImageSource`'s shape and reasons: the Worker does not learn which channel
    a reference came from, and `session_id` picks the binding whose
    credentials fetch it.
    """

    async def file_bytes_for(self, reference: str, session_id: UUID) -> bytes: ...


def unplaced(messages: Sequence[StoredMessage], settled: frozenset[str]) -> list[FileBlock]:
    """Attachments in the Session not yet placed or given up on, oldest first.

    A withdrawn message's file is not placed: taking a message back takes
    back what came with it.
    """
    found: dict[str, FileBlock] = {}
    for stored in messages:
        if stored.withdrawn_at is not None:
            continue
        for block in stored.message.blocks:
            if isinstance(block, FileBlock) and block.reference not in settled:
                found.setdefault(block.reference, block)
    return list(found.values())


def unavailable_note(name: str, reason: str) -> str:
    """What the model is told instead of a file it was promised.

    Without it, a missing inbox file is searched for, guessed at, and then
    described as if it had been read.
    """
    return (
        f"[attachment] {name} could not be fetched into inbox/{name} ({reason}). "
        "Tell the person it did not arrive and ask them to send it again; "
        "do not guess what it contains."
    )


__all__ = ["FileSource", "unavailable_note", "unplaced"]
