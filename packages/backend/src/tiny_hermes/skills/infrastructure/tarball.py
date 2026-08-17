"""Reading a git tarball without ever writing one down.

This is the one module in the trusted process that is allowed to import
`tarfile` for reading somebody else's archive, and `packages/backend/tests/unit/
test_archive_ban.py` names it. That allowance is bought with the four ceilings
below and with one rule that is stronger than any of them: members are read
through `.extractfile()`, which returns a reader and touches no filesystem.
Nothing here extracts a member to a path, and there is no temporary directory
for one to write into, so the whole family of path-traversal bugs that tar
extraction has had over the years has nothing to act on.

Every ceiling is decided from the **header**, before the member's bytes are
read. A limit checked after reading is not a limit: a decompression bomb has
already been decompressed by the time the total is known.

The ceilings here are looser than `domain/package.py`'s on purpose. These bound
what this process will decompress; those bound what may become a skill. A
tarball that passes here and then fails there has cost a few megabytes of
memory, which is the trade being made.
"""

import io
import tarfile

from tiny_hermes.skills.domain.package import MAX_FILE_BYTES, SkillFile

MAX_MEMBERS = 128
#: One member, uncompressed. The same 256 KiB a skill file may be.
MAX_MEMBER_BYTES = MAX_FILE_BYTES
MAX_TOTAL_BYTES = 4 * 1024 * 1024
#: Uncompressed bytes per compressed byte. gzip on prose reaches about 4:1 and
#: on repeated text far more, so 100:1 leaves ordinary archives alone and stops
#: the ones whose only content is repetition.
MAX_COMPRESSION_RATIO = 100


class TarballRefused(Exception):
    """An archive this platform will not read, and why."""


def read_tarball(data: bytes) -> tuple[SkillFile, ...]:
    """The regular files of a gzipped tar, as UTF-8 text.

    Directories are skipped. Anything that is not a regular file — a symlink, a
    hard link, a device node, a fifo — is refused rather than skipped, because
    those members cannot be represented as a skill file at all and silently
    dropping them would import a package that is missing something.
    """
    if not data:
        raise TarballRefused("the archive is empty")
    files: list[SkillFile] = []
    total = 0
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                members += 1
                if members > MAX_MEMBERS:
                    raise TarballRefused(f"more than {MAX_MEMBERS} members")
                path = _member_path(member)
                total = _within_ceilings(member.name, member.size, total, len(data))
                reader = archive.extractfile(member)
                if reader is None:
                    raise TarballRefused(f"{member.name!r} has no content to read")
                files.append(SkillFile(path=path, text=_text(member.name, reader.read())))
    except tarfile.TarError as error:
        raise TarballRefused(f"the archive could not be read: {error}") from error
    if not files:
        raise TarballRefused("the archive holds no files")
    return _strip_root(tuple(files))


def _member_path(member: tarfile.TarInfo) -> str:
    if not member.isfile():
        raise TarballRefused(
            f"{member.name!r} is not a regular file, so it is not a skill file"
        )
    name = member.name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise TarballRefused(f"{member.name!r} is an absolute path")
    if any(part == ".." for part in name.split("/")):
        raise TarballRefused(f"{member.name!r} climbs out of the archive")
    return name


def _within_ceilings(name: str, size: int, total: int, compressed: int) -> int:
    """All four ceilings, from the header, before a byte is decompressed."""
    if size > MAX_MEMBER_BYTES:
        raise TarballRefused(f"{name!r} is larger than {MAX_MEMBER_BYTES} bytes")
    running = total + size
    if running > MAX_TOTAL_BYTES:
        raise TarballRefused(f"the archive unpacks to more than {MAX_TOTAL_BYTES} bytes")
    if running > compressed * MAX_COMPRESSION_RATIO:
        raise TarballRefused(
            f"the archive expands more than {MAX_COMPRESSION_RATIO}:1"
        )
    return running


def _text(name: str, body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as error:
        # §10.4: a skill is UTF-8 text. Refused rather than skipped for the same
        # reason as a symlink — a package quietly missing its diagram is not the
        # package somebody reviewed.
        raise TarballRefused(f"{name!r} is not UTF-8 text") from error


def _strip_root(files: tuple[SkillFile, ...]) -> tuple[SkillFile, ...]:
    """Drop the wrapper directory a git host puts around a repository.

    `codeload` names it `<repo>-<sha>/`, which would otherwise make `SKILL.md`
    live one level down and stop being the manifest. Only stripped when every
    member shares it, so a package that genuinely has two top-level directories
    is left alone and refused by the manifest rules instead.
    """
    roots = {entry.path.split("/", 1)[0] for entry in files}
    if len(roots) != 1 or any("/" not in entry.path for entry in files):
        return files
    return tuple(
        SkillFile(path=entry.path.split("/", 1)[1], text=entry.text) for entry in files
    )
