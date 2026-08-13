"""What an Artifact is: a Run result, not workspace state.

Design §5.5 keeps Artifacts outside the SessionWorkspace on purpose — they
have their own limits, their own retention, and their own authorization
check. Nothing here may import from `session_workspace`.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

DIGEST_HEX_LENGTH = 64


@dataclass(frozen=True)
class Artifact:
    """One stored Run output, addressed by its own identifier.

    ``truncated`` is an honest flag, not an error: a command's output that hit
    the size ceiling is stored up to the limit and marked, so the caller knows
    the bytes are a prefix rather than the whole.
    """

    id: UUID
    workspace_id: UUID
    session_id: UUID
    run_id: UUID
    object_key: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    truncated: bool
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise ValueError(f"negative artifact size: {self.filename}")
        if len(self.sha256) != DIGEST_HEX_LENGTH:
            raise ValueError(f"an artifact needs a content digest: {self.filename}")
        if not self.filename:
            raise ValueError("an artifact needs a filename")
