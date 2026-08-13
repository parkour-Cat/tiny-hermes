"""What a SessionWorkspace is made of, before any store is involved.

A WorkspaceRevision's manifest names every ordinary file and directory and
nothing else — design §3 is explicit that M1 persists no symlink, device, FIFO,
or socket. The refusal lives here, in the type, so a special entry cannot reach
a manifest through any code path rather than through the code paths that
remember to check.
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum

DIGEST_HEX_LENGTH = 64


class UnsupportedWorkspaceEntry(Exception):
    """An entry M1 does not persist, reported rather than silently dropped.

    Dropping it would commit a revision that restores to something other than
    what was scanned; counting it would promise a restore the platform cannot
    perform. Refusing the checkpoint is the only honest answer.
    """


class EntryType(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"

    @classmethod
    def from_scan(cls, raw: str) -> "EntryType":
        """Classify what a scan saw, refusing everything M1 cannot restore."""
        try:
            return cls(raw)
        except ValueError:
            raise UnsupportedWorkspaceEntry(raw) from None


@dataclass(frozen=True)
class WorkspaceEntry:
    """One manifest line: a path, what it is, and what its bytes are.

    ``mode`` may arrive with privilege bits; ``build_manifest`` strips them.
    ``sha256`` is the content hash for a file and ``None`` for a directory —
    enforced here because a manifest entry that lies about which it is would
    corrupt every comparison downstream.
    """

    path: str
    entry_type: EntryType
    mode: int
    size: int
    sha256: str | None

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError(f"negative size: {self.path}")
        if self.entry_type is EntryType.FILE:
            if self.sha256 is None or len(self.sha256) != DIGEST_HEX_LENGTH:
                raise ValueError(f"a file entry needs a content digest: {self.path}")
        else:
            if self.sha256 is not None:
                raise ValueError(f"a directory entry carries no digest: {self.path}")
            if self.size != 0:
                raise ValueError(f"a directory entry carries no size: {self.path}")

    @classmethod
    def file(cls, path: str, content: bytes, *, mode: int) -> "WorkspaceEntry":
        """An entry hashed from the bytes themselves, for tests and staging."""
        return cls(
            path=path,
            entry_type=EntryType.FILE,
            mode=mode,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    @classmethod
    def directory(cls, path: str, *, mode: int) -> "WorkspaceEntry":
        return cls(path=path, entry_type=EntryType.DIRECTORY, mode=mode, size=0, sha256=None)


@dataclass(frozen=True)
class WorkspaceManifest:
    """An immutable, deterministic description of one revision's tree.

    Built only by ``build_manifest``, which owns normalization and ordering.
    ``canonical_bytes`` is the exact document stored in MinIO, so its encoding
    is a compatibility promise, not a formatting preference.
    """

    schema_version: int
    entries: tuple[WorkspaceEntry, ...]
    total_bytes: int
    object_count: int

    def canonical_bytes(self) -> bytes:
        import json  # noqa: PLC0415 - keep the domain module import-light

        return json.dumps(
            {
                "schema_version": self.schema_version,
                "entries": [
                    {
                        "path": entry.path,
                        "type": entry.entry_type.value,
                        "mode": entry.mode,
                        "size": entry.size,
                        "sha256": entry.sha256,
                    }
                    for entry in self.entries
                ],
                "total_bytes": self.total_bytes,
                "object_count": self.object_count,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class WorkspaceQuota:
    """The checkpoint quota — what may become a committed revision.

    Deliberately not called a disk limit: design §1, a command can temporarily
    exceed this while running. The quota decides what is persisted, not what a
    process may write.
    """

    max_bytes: int
    max_objects: int


class CheckpointStatus(StrEnum):
    UNCHANGED = "unchanged"
    COMMITTED = "committed"
    LIMIT_EXCEEDED = "limit_exceeded"
    CONFLICT = "conflict"
    STORAGE_FAILED = "storage_failed"


class UploadKind(StrEnum):
    WORKSPACE = "workspace"
    ARTIFACT = "artifact"


class UploadStatus(StrEnum):
    """Design §6.2 — the only rows GC may touch are the ones marked reclaimable.

    ``FINALIZING`` and ``READY`` are GC roots: their candidate index is durable
    and enumerates final keys that must survive a concurrent collector.
    """

    UPLOADING = "uploading"
    FINALIZING = "finalizing"
    READY = "ready"
    COMMITTED = "committed"
    ABANDONED = "abandoned"
    EXPIRED = "expired"
