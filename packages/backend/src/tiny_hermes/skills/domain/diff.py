"""What one proposal would change, computed the same way every time.

Product design §15.3 step 2. This is the thing an approver actually reads
before they decide, which sets two requirements that shape the whole module.

**It is deterministic.** Two people opening the same proposal, or the same
person opening it twice, must see the same document — a diff that reordered
itself between reads would make "I reviewed this" mean nothing. So the files
are sorted by path, the line matching is `difflib`'s, and no part of the result
depends on dictionary order or on when it was computed.

**It is pure.** Nothing here reads the catalog. It takes two file sets and
returns the difference, which is what makes it testable line by line and what
lets the same function serve the console, the approval check and a test.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from tiny_hermes.skills.domain.package import SkillFile

#: Unchanged lines kept either side of a change. Three is what `diff -u` shows
#: and what a reviewer needs to place a hunk in a file they half remember.
CONTEXT_LINES = 3

#: Where one file's diff stops and says so. A person reviewing a 4,000-line
#: replacement is not reading it line by line, and a proposal list that has to
#: carry every line of every file becomes a response nothing can render. This
#: is not the "refuse rather than truncate" rule from the context planner: that
#: one protects a model that cannot tell it was given half a document, and this
#: is a person who is told, in the same object, that there is more.
MAX_FILE_DIFF_LINES = 400


class FileChange(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class LineKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"
    #: One entry standing in for the lines between two hunks, so a reader can
    #: see that something was skipped rather than infer it from line numbers.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DiffLine:
    kind: LineKind
    text: str


@dataclass(frozen=True)
class FileDiff:
    path: str
    change: FileChange
    lines: tuple[DiffLine, ...]
    added_lines: int
    removed_lines: int
    #: True when this file's diff stopped at `MAX_FILE_DIFF_LINES`. The counts
    #: above are still the whole file's, so a truncated diff never understates
    #: how much changed.
    truncated: bool = False


@dataclass(frozen=True)
class PackageDiff:
    """Every file that differs, in path order. Unchanged files are absent."""

    files: tuple[FileDiff, ...]

    @property
    def added(self) -> int:
        return sum(1 for item in self.files if item.change is FileChange.ADDED)

    @property
    def removed(self) -> int:
        return sum(1 for item in self.files if item.change is FileChange.REMOVED)

    @property
    def changed(self) -> int:
        return sum(1 for item in self.files if item.change is FileChange.CHANGED)

    @property
    def empty(self) -> bool:
        """True when the two packages hold the same content.

        Worth naming: a proposal against a base it does not change is a
        proposal there is nothing to approve, and the caller says so rather
        than showing a reviewer an empty page.
        """
        return not self.files


def diff_packages(
    base: Sequence[SkillFile], proposed: Sequence[SkillFile]
) -> PackageDiff:
    """The difference between two file sets, by path then by line.

    An empty `base` is how a brand new skill is diffed: every file reads as
    added, which is exactly what a reviewer of a new skill is looking at.
    """
    before = {item.path: item.text for item in base}
    after = {item.path: item.text for item in proposed}
    files: list[FileDiff] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None:
            files.append(_whole(path, new or "", FileChange.ADDED, LineKind.ADDED))
        elif new is None:
            files.append(_whole(path, old, FileChange.REMOVED, LineKind.REMOVED))
        else:
            files.append(_changed(path, old, new))
    return PackageDiff(files=tuple(files))


def _lines(text: str) -> list[str]:
    """A file's lines, without the trailing empty one a final newline makes.

    `"a\\n"` is one line, not two. Counting the phantom would report every
    ordinary file as having a blank line nobody wrote.
    """
    split = text.split("\n")
    if split and split[-1] == "":
        split.pop()
    return split


def _whole(path: str, text: str, change: FileChange, kind: LineKind) -> FileDiff:
    body = _lines(text)
    lines = tuple(DiffLine(kind=kind, text=line) for line in body[:MAX_FILE_DIFF_LINES])
    return FileDiff(
        path=path,
        change=change,
        lines=lines,
        added_lines=len(body) if kind is LineKind.ADDED else 0,
        removed_lines=len(body) if kind is LineKind.REMOVED else 0,
        truncated=len(body) > MAX_FILE_DIFF_LINES,
    )


def _changed(path: str, old: str, new: str) -> FileDiff:
    before = _lines(old)
    after = _lines(new)
    lines: list[DiffLine] = []
    added = 0
    removed = 0
    truncated = False
    for tag, first, last, other_first, other_last in SequenceMatcher(
        a=before, b=after, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            lines.extend(_context(before[first:last]))
            continue
        if tag in {"replace", "delete"}:
            removed += last - first
            lines.extend(
                DiffLine(kind=LineKind.REMOVED, text=line) for line in before[first:last]
            )
        if tag in {"replace", "insert"}:
            added += other_last - other_first
            lines.extend(
                DiffLine(kind=LineKind.ADDED, text=line)
                for line in after[other_first:other_last]
            )
    if len(lines) > MAX_FILE_DIFF_LINES:
        truncated = True
        del lines[MAX_FILE_DIFF_LINES:]
    return FileDiff(
        path=path,
        change=FileChange.CHANGED,
        lines=tuple(lines),
        added_lines=added,
        removed_lines=removed,
        truncated=truncated,
    )


def _context(equal: Sequence[str]) -> list[DiffLine]:
    """The unchanged lines worth showing, and a marker for the rest.

    A long unchanged stretch collapses to its edges. The marker is an entry of
    its own rather than an omission, because a reader who cannot see that lines
    were skipped will read two distant hunks as adjacent.
    """
    if len(equal) <= CONTEXT_LINES * 2 + 1:
        return [DiffLine(kind=LineKind.CONTEXT, text=line) for line in equal]
    skipped = len(equal) - CONTEXT_LINES * 2
    return [
        *(DiffLine(kind=LineKind.CONTEXT, text=line) for line in equal[:CONTEXT_LINES]),
        DiffLine(kind=LineKind.SKIPPED, text=f"{skipped} unchanged lines"),
        *(DiffLine(kind=LineKind.CONTEXT, text=line) for line in equal[-CONTEXT_LINES:]),
    ]
