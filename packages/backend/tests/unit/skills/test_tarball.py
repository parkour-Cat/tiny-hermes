"""What the import reader refuses, member by member.

The four ceilings are asserted from the header side: every archive built here
declares a size, and the reader has to decide before it reads. A test that only
checked the outcome would pass just as well against a reader that decompresses
first and complains afterwards, which is the bug the ceilings exist to prevent.
"""

import io
import os
import tarfile

import pytest
from tiny_hermes.skills.infrastructure.tarball import (
    MAX_COMPRESSION_RATIO,
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    MAX_TOTAL_BYTES,
    TarballRefused,
    read_tarball,
)

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes
"""


def archive(
    files: dict[str, bytes] | None = None,
    *,
    extra: list[tarfile.TarInfo] | None = None,
    root: str = "repo-abc123",
) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for path, body in (files or {"SKILL.md": SKILL_MD.encode()}).items():
            info = tarfile.TarInfo(name=f"{root}/{path}" if root else path)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        for info in extra or []:
            tar.addfile(info)
    return raw.getvalue()


def link(name: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.type = kind
    info.linkname = "../../etc/passwd"
    return info


def test_a_tarball_becomes_files_without_its_wrapper_directory() -> None:
    """`codeload` wraps everything in `<repo>-<sha>/`, which would otherwise
    stop `SKILL.md` from being at the package root."""
    files = read_tarball(
        archive({"SKILL.md": SKILL_MD.encode(), "style.md": b"Short sentences."})
    )
    assert {entry.path for entry in files} == {"SKILL.md", "style.md"}


def test_two_top_level_directories_keep_their_names() -> None:
    """Stripping unconditionally would silently reshape a package that meant
    to have two directories. It is left alone and refused by the manifest."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for path in ("one/a.md", "two/b.md"):
            info = tarfile.TarInfo(name=path)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    files = read_tarball(raw.getvalue())
    assert {entry.path for entry in files} == {"one/a.md", "two/b.md"}


def test_a_symlink_member_is_refused_rather_than_skipped() -> None:
    """Every historical `extractall` hole is one of these. Here it is refused
    for being unrepresentable as a skill file, which is a smaller claim and a
    stronger one — nothing is written to disk for it to escape from."""
    with pytest.raises(TarballRefused, match="not a regular file"):
        read_tarball(archive(extra=[link("repo-abc123/passwd")]))


def test_a_hard_link_member_is_refused() -> None:
    with pytest.raises(TarballRefused, match="not a regular file"):
        read_tarball(archive(extra=[link("repo-abc123/hard", tarfile.LNKTYPE)]))


def test_a_device_node_is_refused() -> None:
    with pytest.raises(TarballRefused, match="not a regular file"):
        read_tarball(archive(extra=[link("repo-abc123/null", tarfile.CHRTYPE)]))


def test_an_absolute_member_name_is_refused() -> None:
    with pytest.raises(TarballRefused, match="absolute path"):
        read_tarball(archive({"SKILL.md": b"x"}, root="/etc"))


def test_a_member_climbing_out_is_refused() -> None:
    with pytest.raises(TarballRefused, match="climbs out"):
        read_tarball(archive({"../../etc/passwd": b"x"}, root=""))


def test_more_members_than_the_ceiling_is_refused() -> None:
    too_many = {f"file-{index}.md": b"x" for index in range(MAX_MEMBERS + 1)}
    with pytest.raises(TarballRefused, match=f"more than {MAX_MEMBERS} members"):
        read_tarball(archive(too_many))


def test_one_oversized_member_is_refused_from_its_header() -> None:
    """`member.size` is read out of the header, so this costs nothing to
    decide — which is the point of deciding it here."""
    with pytest.raises(TarballRefused, match="larger than"):
        read_tarball(archive({"big.md": b"a" * (MAX_MEMBER_BYTES + 1)}))


def test_members_that_add_up_past_the_ceiling_are_refused() -> None:
    """Each member is under its own ceiling and the archive still is not.

    The bodies are hex, so they compress about 2:1 and stay well clear of the
    ratio ceiling — otherwise this would pass for the wrong reason and the two
    ceilings would be indistinguishable from one.
    """
    body = os.urandom(MAX_MEMBER_BYTES // 2 - 1).hex().encode()
    count = MAX_TOTAL_BYTES // len(body) + 2
    files = {f"file-{index}.md": body for index in range(count)}
    with pytest.raises(TarballRefused, match="unpacks to more than"):
        read_tarball(archive(files))


def test_an_archive_that_expands_too_far_is_refused() -> None:
    """A bomb: one member of zeroes that gzip stores in almost nothing. Both
    this and the total ceiling would catch a large enough one; this catches the
    small ones, which is where the total alone would let a caller through."""
    zeros = b"\0" * (MAX_MEMBER_BYTES - 1)
    data = archive({f"file-{index}.md": zeros for index in range(3)})
    assert len(data) * MAX_COMPRESSION_RATIO < 3 * len(zeros)
    with pytest.raises(TarballRefused, match=f"more than {MAX_COMPRESSION_RATIO}:1"):
        read_tarball(data)


def test_a_member_that_is_not_utf_8_is_refused() -> None:
    with pytest.raises(TarballRefused, match="not UTF-8"):
        read_tarball(archive({"logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe"}))


def test_something_that_is_not_a_tarball_is_refused() -> None:
    with pytest.raises(TarballRefused, match="could not be read"):
        read_tarball(b"<!doctype html><title>404</title>")


def test_an_empty_body_is_refused() -> None:
    with pytest.raises(TarballRefused, match="empty"):
        read_tarball(b"")
