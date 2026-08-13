"""The manifest is the workspace's word, so it has to be deterministic.

Pure functions, exhaustively tested before anything touches PostgreSQL, MinIO,
or a container — the same shape as the address policy in 3A and the container
policy in 3B. A wrong answer here is a revision that would have lied about its
own contents.
"""

import hashlib

import pytest
from tiny_hermes.session_workspace.domain.manifest import (
    DuplicateWorkspacePath,
    InvalidWorkspacePath,
    UnsupportedWorkspaceEntry,
    build_manifest,
    measure,
    normalize_workspace_path,
    project_write,
)
from tiny_hermes.session_workspace.domain.models import (
    EntryType,
    WorkspaceEntry,
    WorkspaceQuota,
)


def entries_with(*, bytes_total: int, objects: int) -> tuple[WorkspaceEntry, ...]:
    """`objects` entries whose file sizes sum to `bytes_total`."""
    if objects < 1:
        return ()
    sizes = [0] * objects
    sizes[0] = bytes_total
    return tuple(
        WorkspaceEntry(
            path=f"f{index}.bin",
            entry_type=EntryType.FILE,
            mode=0o644,
            size=size,
            sha256="0" * 64,
        )
        for index, size in enumerate(sizes)
    )


# -- Determinism ------------------------------------------------------------


def test_manifest_is_nfc_sorted_and_hash_is_deterministic() -> None:
    first = WorkspaceEntry.file("z.txt", b"z", mode=0o664)
    # e + combining acute: NFC folds it into é, and é (U+00E9) sorts after z
    # bytewise in UTF-8, which is the order the manifest promises.
    second = WorkspaceEntry.file("e\u0301.txt", "é".encode(), mode=0o4755)
    manifest = build_manifest((first, second), schema_version=1)
    assert [entry.path for entry in manifest.entries] == ["z.txt", "é.txt"]
    assert manifest.sha256 == hashlib.sha256(manifest.canonical_bytes()).hexdigest()


def test_the_same_entries_in_any_order_produce_the_same_bytes() -> None:
    one = WorkspaceEntry.file("a.txt", b"a", mode=0o644)
    two = WorkspaceEntry.directory("sub", mode=0o755)
    three = WorkspaceEntry.file("sub/b.txt", b"bb", mode=0o600)
    forward = build_manifest((one, two, three), schema_version=1)
    backward = build_manifest((three, two, one), schema_version=1)
    assert forward.canonical_bytes() == backward.canonical_bytes()
    assert forward.sha256 == backward.sha256


def test_canonical_bytes_are_compact_sorted_utf8_json() -> None:
    manifest = build_manifest(
        (WorkspaceEntry.file("é.txt", b"x", mode=0o644),), schema_version=1
    )
    body = manifest.canonical_bytes().decode("utf-8")
    assert ": " not in body and ", " not in body
    # Real UTF-8, not \u escapes: the path bytes are the sort key, so the
    # document carries them as themselves.
    assert "é.txt" in body


def test_file_hashes_are_content_hashes() -> None:
    entry = WorkspaceEntry.file("a.txt", b"hello", mode=0o644)
    assert entry.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert entry.size == 5


# -- Mode normalization -----------------------------------------------------


def test_privilege_bits_are_removed_and_the_lower_nine_kept() -> None:
    manifest = build_manifest(
        (
            WorkspaceEntry.file("suid.bin", b"x", mode=0o4755),
            WorkspaceEntry.file("sgid.bin", b"x", mode=0o2711),
            WorkspaceEntry.directory("sticky", mode=0o1777),
        ),
        schema_version=1,
    )
    modes = {entry.path: entry.mode for entry in manifest.entries}
    assert modes == {"suid.bin": 0o755, "sgid.bin": 0o711, "sticky": 0o777}


# -- Path rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../x", "a/../x", "a//x", "a/./x", "a\\x", "a\x00x", "", ".", ".."],
)
def test_invalid_workspace_path_is_refused(path: str) -> None:
    with pytest.raises(InvalidWorkspacePath):
        normalize_workspace_path(path)


def test_paths_normalize_to_nfc() -> None:
    assert normalize_workspace_path("e\u0301.txt") == "é.txt"
    assert normalize_workspace_path("a/b.txt") == "a/b.txt"


def test_a_filename_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(InvalidWorkspacePath):
        normalize_workspace_path(b"\xff\xfe.txt")


def test_bytes_paths_are_decoded_strictly_then_normalized() -> None:
    assert normalize_workspace_path("é.txt".encode()) == "é.txt"


def test_two_paths_equal_after_normalization_are_a_duplicate_error() -> None:
    with pytest.raises(DuplicateWorkspacePath):
        build_manifest(
            (
                WorkspaceEntry.file("e\u0301.txt", b"a", mode=0o644),
                WorkspaceEntry.file("é.txt", b"b", mode=0o644),
            ),
            schema_version=1,
        )


# -- Entry types --------------------------------------------------------------


@pytest.mark.parametrize("raw", ["symlink", "hardlink", "device", "fifo", "socket"])
def test_special_entries_are_refused_not_counted(raw: str) -> None:
    with pytest.raises(UnsupportedWorkspaceEntry):
        EntryType.from_scan(raw)


def test_files_and_directories_are_the_supported_entries() -> None:
    assert EntryType.from_scan("file") is EntryType.FILE
    assert EntryType.from_scan("directory") is EntryType.DIRECTORY


# -- Quota arithmetic ---------------------------------------------------------


def test_one_byte_and_one_object_over_limit_are_distinct() -> None:
    quota = WorkspaceQuota(max_bytes=100, max_objects=2)
    within = build_manifest(entries_with(bytes_total=100, objects=2), schema_version=1)
    over_bytes = build_manifest(entries_with(bytes_total=101, objects=2), schema_version=1)
    over_objects = build_manifest(entries_with(bytes_total=100, objects=3), schema_version=1)
    assert measure(within, quota).allowed
    assert measure(within, quota).dimension is None
    assert not measure(over_bytes, quota).allowed
    assert measure(over_bytes, quota).dimension == "bytes"
    assert not measure(over_objects, quota).allowed
    assert measure(over_objects, quota).dimension == "objects"


def test_directories_count_toward_the_object_limit() -> None:
    manifest = build_manifest(
        (
            WorkspaceEntry.directory("sub", mode=0o755),
            WorkspaceEntry.file("sub/a.txt", b"aa", mode=0o644),
        ),
        schema_version=1,
    )
    assert manifest.object_count == 2
    assert manifest.total_bytes == 2


def test_an_empty_manifest_measures_clean() -> None:
    manifest = build_manifest((), schema_version=1)
    assert manifest.object_count == 0
    assert manifest.total_bytes == 0
    assert measure(manifest, WorkspaceQuota(max_bytes=0, max_objects=0)).allowed


# -- file.write replacement delta --------------------------------------------


def test_replacing_a_file_projects_the_size_delta_not_the_sum() -> None:
    manifest = build_manifest(
        (WorkspaceEntry.file("a.txt", b"aaaa", mode=0o644),), schema_version=1
    )
    projected = project_write(manifest, "a.txt", 10)
    assert projected.total_bytes == 10
    assert projected.object_count == 1


def test_a_new_file_adds_its_bytes_and_missing_parent_directories() -> None:
    manifest = build_manifest(
        (WorkspaceEntry.file("a.txt", b"aaaa", mode=0o644),), schema_version=1
    )
    projected = project_write(manifest, "deep/nested/b.txt", 6)
    # a.txt + deep + deep/nested + b.txt
    assert projected.total_bytes == 10
    assert projected.object_count == 4


def test_a_write_into_an_existing_directory_adds_only_the_file() -> None:
    manifest = build_manifest(
        (
            WorkspaceEntry.directory("sub", mode=0o755),
            WorkspaceEntry.file("sub/a.txt", b"aa", mode=0o644),
        ),
        schema_version=1,
    )
    projected = project_write(manifest, "sub/b.txt", 3)
    assert projected.total_bytes == 5
    assert projected.object_count == 3


def test_projected_totals_measure_against_the_quota() -> None:
    manifest = build_manifest(
        (WorkspaceEntry.file("a.txt", b"aaaa", mode=0o644),), schema_version=1
    )
    quota = WorkspaceQuota(max_bytes=10, max_objects=1)
    assert measure(project_write(manifest, "a.txt", 10), quota).allowed
    assert measure(project_write(manifest, "a.txt", 11), quota).dimension == "bytes"
    assert measure(project_write(manifest, "b.txt", 1), quota).dimension == "objects"


# -- Entry validation ---------------------------------------------------------


def test_a_file_entry_requires_a_digest_and_a_directory_refuses_one() -> None:
    with pytest.raises(ValueError, match="digest"):
        WorkspaceEntry(
            path="a.txt", entry_type=EntryType.FILE, mode=0o644, size=1, sha256=None
        )
    with pytest.raises(ValueError, match="digest"):
        WorkspaceEntry(
            path="d", entry_type=EntryType.DIRECTORY, mode=0o755, size=0, sha256="0" * 64
        )


def test_a_directory_carries_no_bytes() -> None:
    with pytest.raises(ValueError, match="size"):
        WorkspaceEntry(
            path="d", entry_type=EntryType.DIRECTORY, mode=0o755, size=3, sha256=None
        )


def test_negative_sizes_are_refused() -> None:
    with pytest.raises(ValueError, match="size"):
        WorkspaceEntry(
            path="a.txt", entry_type=EntryType.FILE, mode=0o644, size=-1, sha256="0" * 64
        )
