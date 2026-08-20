"""What a skill package is, decided before anything is stored.

Product design §15.1. Pure: nothing here opens a socket, reads a database, or
touches a filesystem. It takes the files somebody handed over and either returns
one comparable package or names what is wrong with it.

Two things worth stating once.

**Every refusal names the file.** Whoever gets one is holding a directory and
has to know which file to change; a single "invalid package" sends them reading
their own upload byte by byte.

**The frontmatter parser is deliberately tiny.** A YAML parser is a large
surface to point at content an administrator uploaded, and a skill manifest
needs two strings. So this reads `key: value` lines between two `---` markers
and refuses everything else, rather than accepting a document language and
hoping the interesting parts of it never matter.
"""

import hashlib
import re
from dataclasses import dataclass

from tiny_hermes.session_workspace.domain.manifest import (
    InvalidWorkspacePath,
    normalize_workspace_path,
)

#: The manifest, at the package root. One spelling, because it is also the
#: default argument of `skill.load` and a model should not have to guess.
MANIFEST_PATH = "SKILL.md"

MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
#: Tighter than the files beside it. The manifest is the one file a person reads
#: in a review and the one file whose body a Run loads by default.
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024

NAME_MAX_LENGTH = 64
#: Arithmetic, not taste. §7.4.2 gives the skill-summaries segment 1536 tokens
#: at most, an Agent may bind 16 skills, and 1536 / 16 is 96 tokens — which the
#: conservative estimator reaches at roughly 200 characters. A longer cap here
#: would produce Agents that cannot be published, and the author would find out
#: at publish rather than while writing the skill.
SUMMARY_MAX_LENGTH = 200

#: Same shape as an Agent alias: it is typed by a model into `skill.load`, and a
#: name needing quoting is a name that will arrive misquoted.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


class SkillPackageRefused(Exception):
    """A package this platform will not store, and why."""


@dataclass(frozen=True)
class SkillFile:
    path: str
    text: str

    @property
    def bytes_used(self) -> int:
        return len(self.text.encode("utf-8"))


@dataclass(frozen=True)
class SkillManifest:
    """What the system prompt is allowed to say about a skill.

    Only these two. §15.2 says the prompt lists names and summaries, so a
    manifest field nobody shows would be a field whose cost nobody measured.
    """

    name: str
    description: str


@dataclass(frozen=True)
class SkillPackage:
    manifest: SkillManifest
    #: Sorted by path, newline-normalized, and deduplicated — the form the
    #: content hash is taken over.
    files: tuple[SkillFile, ...]
    content_hash: str
    total_bytes: int


def content_hash(files: tuple[SkillFile, ...]) -> str:
    """The identity of a package's content, independent of how it arrived.

    Length-prefixed rather than separated, so no path or body can be written to
    look like the end of the one before it. Sorted, so an upload and an import
    of the same files agree — which is the whole of "the same content does not
    make a second version".
    """
    digest = hashlib.sha256()
    for entry in sorted(files, key=lambda item: item.path):
        path = entry.path.encode("utf-8")
        body = entry.text.encode("utf-8")
        digest.update(f"{len(path)}:".encode())
        digest.update(path)
        digest.update(f"{len(body)}:".encode())
        digest.update(body)
    return digest.hexdigest()


def parse_package(files: tuple[SkillFile, ...]) -> SkillPackage:
    """One comparable package, or a named refusal."""
    if len(files) > MAX_FILES:
        raise SkillPackageRefused(
            f"a skill package may hold {MAX_FILES} files, not {len(files)}"
        )

    seen: dict[str, SkillFile] = {}
    total = 0
    for entry in files:
        path = _clean_path(entry.path)
        if path in seen:
            # Which of the two bodies won would be an accident of ordering.
            raise SkillPackageRefused(f"a path may appear once: {path}")
        text = _clean_text(path, entry.text)
        cleaned = SkillFile(path=path, text=text)
        _check_size(cleaned)
        seen[path] = cleaned
        total += cleaned.bytes_used

    if total > MAX_PACKAGE_BYTES:
        raise SkillPackageRefused(
            f"a skill package may total {MAX_PACKAGE_BYTES} bytes, not {total}"
        )
    if MANIFEST_PATH not in seen:
        raise SkillPackageRefused(f"a skill package needs a {MANIFEST_PATH} at its root")

    ordered = tuple(seen[path] for path in sorted(seen))
    return SkillPackage(
        manifest=_parse_manifest(seen[MANIFEST_PATH].text),
        files=ordered,
        content_hash=content_hash(ordered),
        total_bytes=total,
    )


def _clean_path(path: str) -> str:
    try:
        return normalize_workspace_path(path)
    except InvalidWorkspacePath as refused:
        # The package's paths and a committed workspace's paths obey one rule
        # and one implementation. A second one here would drift.
        raise SkillPackageRefused(f"path {path!r}: {refused}") from refused


def _clean_text(path: str, text: str) -> str:
    if "\x00" in text:
        # PostgreSQL text cannot hold one, so this would be a store failure
        # somewhere far from the file that caused it.
        raise SkillPackageRefused(f"{path} contains a NUL byte")
    # Normalized so the same file uploaded from Windows and imported from a tar
    # hash the same. Skills are text served into a prompt; nothing downstream
    # can tell the two spellings apart, and leaving them different would make
    # "the same content" untrue for a reason nobody could see.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _check_size(entry: SkillFile) -> None:
    used = entry.bytes_used
    # Bytes, not characters: a cap counted in characters is three times looser
    # for Chinese text than for English.
    if entry.path == MANIFEST_PATH and used > MAX_SKILL_MD_BYTES:
        raise SkillPackageRefused(
            f"{MANIFEST_PATH} is {used} bytes; a manifest may be {MAX_SKILL_MD_BYTES}"
        )
    if used > MAX_FILE_BYTES:
        raise SkillPackageRefused(
            f"{entry.path} is {used} bytes; a skill file may be {MAX_FILE_BYTES}"
        )


def _parse_manifest(text: str) -> SkillManifest:
    found = _FRONTMATTER.match(text)
    if found is None:
        raise SkillPackageRefused(
            f"{MANIFEST_PATH} must open with a --- frontmatter block"
        )
    fields: dict[str, str] = {}
    for line in found.group(1).splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillPackageRefused(f"{MANIFEST_PATH} frontmatter: {line.strip()!r}")
        key = key.strip()
        if key in fields:
            raise SkillPackageRefused(f"{MANIFEST_PATH} frontmatter states {key} twice")
        fields[key] = value.strip().strip("\"'")

    unknown = sorted(set(fields) - {"name", "description"})
    if unknown:
        # Refused rather than ignored: a field the platform drops is a field its
        # author believes is doing something.
        raise SkillPackageRefused(
            f"{MANIFEST_PATH} frontmatter: unknown {', '.join(unknown)}"
        )
    return SkillManifest(name=_valid_name(fields), description=_valid_description(fields))


def _valid_name(fields: dict[str, str]) -> str:
    name = fields.get("name", "")
    if not name:
        raise SkillPackageRefused(f"{MANIFEST_PATH} frontmatter has no name")
    if len(name) > NAME_MAX_LENGTH:
        raise SkillPackageRefused(f"a skill name may be {NAME_MAX_LENGTH} characters")
    if not NAME_PATTERN.match(name):
        raise SkillPackageRefused(f"a skill name looks like release-notes, not {name!r}")
    return name


def _valid_description(fields: dict[str, str]) -> str:
    description = fields.get("description", "")
    if not description:
        raise SkillPackageRefused(f"{MANIFEST_PATH} frontmatter has no description")
    if len(description) > SUMMARY_MAX_LENGTH:
        raise SkillPackageRefused(
            f"a description may be {SUMMARY_MAX_LENGTH} characters, not {len(description)}"
        )
    return description
