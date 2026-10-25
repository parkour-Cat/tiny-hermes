"""Sending a saved workspace file to the person (§1.27, v2.27)."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

#: Feishu's own ceiling for an uploaded file.
MAX_SEND_BYTES = 30 * 1024 * 1024

#: Per Run. A task that hands back more than ten files is better as one
#: archive, and a loop that sends the same file forever stops here.
MAX_FILES_PER_RUN = 10


@dataclass(frozen=True)
class FileSent:
    name: str
    size: int
    #: False when the conversation is not one a file can be sent into (the
    #: console, the web chat): the file stays in the workspace to download.
    delivered: bool


class FileSendRefused(Exception):
    """Not sent, for a reason the model can act on."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class FileSender(Protocol):
    async def send(
        self,
        *,
        workspace_id: UUID,
        session_id: UUID,
        run_id: UUID,
        path: str,
        delivery_key: str,
    ) -> FileSent: ...


__all__ = ["MAX_FILES_PER_RUN", "MAX_SEND_BYTES", "FileSendRefused", "FileSender", "FileSent"]
