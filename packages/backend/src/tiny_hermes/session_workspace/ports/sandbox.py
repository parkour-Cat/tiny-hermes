"""The sandbox, as the SessionWorkspace needs it: bytes and metadata, bound.

A port instance is already bound to one Run, lease, and sandbox by whoever
constructed it — the service asks for a scan or moves a stream and never
handles an identifier, so it cannot mix two sandboxes up. No Docker type and
no transport frame crosses this boundary (design §5.1).
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ScanEntry:
    """One tree member as the Controller's scan reported it, unjudged.

    ``entry_type`` is the scanner's raw word (``file``, ``directory``,
    ``symlink``, ``device``, ...). Judging it — refusing what M1 cannot
    restore — is the domain's job, in one place.
    """

    path: str
    entry_type: str
    mode: int
    size: int
    sha256: str | None


@dataclass(frozen=True)
class ExportedFile:
    """One file body leaving the sandbox, streamed rather than held."""

    path: str
    size: int
    chunks: AsyncIterator[bytes]


class SandboxWorkspacePort(Protocol):
    async def scan(self) -> tuple[ScanEntry, ...]:
        """Metadata for the frozen data tree: paths, types, modes, digests."""
        ...

    async def import_tree(
        self, tar_stream: AsyncIterator[bytes], *, declared_total: int
    ) -> None:
        """Extract a platform-built tar into the frozen, empty data mount."""
        ...

    def export_files(self, paths: Sequence[str]) -> AsyncIterator[ExportedFile]:
        """The named file bodies, streamed one after another.

        Which paths are worth exporting is the caller's arithmetic (changed
        digests); how the bytes leave the container is this port's.
        """
        ...
