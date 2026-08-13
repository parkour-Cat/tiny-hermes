"""Normalization, ordering, hashing, and quota arithmetic. Pure on purpose.

Every decision that makes two manifests comparable is made here and nowhere
else: NFC paths, bytewise ordering, privilege-bit stripping, and the totals the
quota is measured against. The scan, the import, and the tools all call these
same functions, which is what makes "the restored tree equals the manifest" a
comparison rather than a hope.
"""

import unicodedata
from dataclasses import dataclass
from typing import Literal, Protocol

from tiny_hermes.session_workspace.domain.models import (
    EntryType,
    UnsupportedWorkspaceEntry,
    WorkspaceEntry,
    WorkspaceManifest,
    WorkspaceQuota,
)

#: setuid, setgid, and sticky are discarded at checkpoint — design §6.5. The
#: lower nine bits are what a restore may faithfully reproduce.
MODE_KEPT_BITS = 0o777


class InvalidWorkspacePath(Exception):
    """A path no committed workspace may contain."""


class DuplicateWorkspacePath(Exception):
    """Two source paths that became the same path after normalization.

    Committing either one silently would restore a tree that differs from what
    was scanned; which of the two bodies wins would be an accident of ordering.
    """


def normalize_workspace_path(path: str | bytes) -> str:
    """UTF-8 NFC, `/` separators, no empty, `.`, or `..` segment.

    Bytes arrive from a Linux filesystem that promises nothing about encoding;
    a name that is not valid UTF-8 is refused at checkpoint (design §6.5)
    rather than stored as an approximation of itself.
    """
    if isinstance(path, bytes):
        try:
            path = path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as refused:
            raise InvalidWorkspacePath("not valid UTF-8") from refused
    if not path:
        raise InvalidWorkspacePath("empty path")
    if "\x00" in path:
        raise InvalidWorkspacePath("NUL in path")
    if "\\" in path:
        # A backslash is an ordinary byte to Linux and a separator to enough
        # other software that a path carrying one is a confusion in waiting.
        raise InvalidWorkspacePath("backslash in path")
    if path.startswith("/"):
        raise InvalidWorkspacePath("absolute path")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise InvalidWorkspacePath(f"illegal segment in: {path}")
    return unicodedata.normalize("NFC", path)


def build_manifest(
    entries: tuple[WorkspaceEntry, ...], *, schema_version: int
) -> WorkspaceManifest:
    """The one constructor of a comparable manifest.

    Normalizes every path and mode, refuses duplicates, orders bytewise by
    normalized UTF-8 path bytes, and computes the totals the quota reads.
    """
    normalized: dict[str, WorkspaceEntry] = {}
    for entry in entries:
        path = normalize_workspace_path(entry.path)
        if path in normalized:
            raise DuplicateWorkspacePath(path)
        normalized[path] = WorkspaceEntry(
            path=path,
            entry_type=entry.entry_type,
            mode=entry.mode & MODE_KEPT_BITS,
            size=entry.size,
            sha256=entry.sha256,
        )

    ordered = tuple(
        normalized[path] for path in sorted(normalized, key=lambda p: p.encode("utf-8"))
    )
    return WorkspaceManifest(
        schema_version=schema_version,
        entries=ordered,
        total_bytes=sum(e.size for e in ordered),
        object_count=len(ordered),
    )


class _HasTotals(Protocol):
    @property
    def total_bytes(self) -> int: ...

    @property
    def object_count(self) -> int: ...


@dataclass(frozen=True)
class QuotaMeasurement:
    """One measurement against one quota, naming the dimension that failed.

    ``dimension`` is reported so the limit event can say *which* ceiling was
    crossed without listing private filenames — design §9.
    """

    allowed: bool
    dimension: Literal["bytes", "objects"] | None
    total_bytes: int
    object_count: int


def measure(totals: _HasTotals, quota: WorkspaceQuota) -> QuotaMeasurement:
    """Exactly at the limit passes; one unit over refuses. Bytes reported first."""
    dimension: Literal["bytes", "objects"] | None = None
    if totals.total_bytes > quota.max_bytes:
        dimension = "bytes"
    elif totals.object_count > quota.max_objects:
        dimension = "objects"
    return QuotaMeasurement(
        allowed=dimension is None,
        dimension=dimension,
        total_bytes=totals.total_bytes,
        object_count=totals.object_count,
    )


@dataclass(frozen=True)
class ProjectedTotals:
    """What the workspace would total after one write, for the preflight.

    `file.write` refuses before touching the file when these exceed the quota
    (design §9); `shell.exec` cannot be projected and is measured afterwards.
    """

    total_bytes: int
    object_count: int


def project_write(
    manifest: WorkspaceManifest, path: str | bytes, size: int
) -> ProjectedTotals:
    """The replacement delta: new totals if `path` became a `size`-byte file.

    Replacing counts the difference, not the sum. A new file also creates every
    missing ancestor directory, and directories count toward the object limit.
    """
    target = normalize_workspace_path(path)
    existing = {entry.path: entry for entry in manifest.entries}

    replaced = existing.get(target)
    total_bytes = manifest.total_bytes - (replaced.size if replaced else 0) + size
    object_count = manifest.object_count + (0 if replaced else 1)

    if replaced is None:
        parent_parts = target.split("/")[:-1]
        ancestor = ""
        for part in parent_parts:
            ancestor = f"{ancestor}/{part}" if ancestor else part
            if ancestor not in existing:
                object_count += 1

    return ProjectedTotals(total_bytes=total_bytes, object_count=object_count)


__all__ = [
    "MODE_KEPT_BITS",
    "DuplicateWorkspacePath",
    "EntryType",
    "InvalidWorkspacePath",
    "ProjectedTotals",
    "QuotaMeasurement",
    "UnsupportedWorkspaceEntry",
    "build_manifest",
    "measure",
    "normalize_workspace_path",
    "project_write",
]
