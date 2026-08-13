"""The openat2 helper against a real kernel, symlink races included.

Design §5.2: `file.*` never trusts a checked path string. The helper binary
inside the runtime image opens `/workspace/data` once and resolves every
descendant with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS)` — the kernel refuses the traversal, so there is no
window between a check and a use for an attacker to swap a directory into a
symlink. These tests exec the helper directly, including under exactly that
race.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

HELPER = "/usr/local/bin/tiny-hermes-file-helper"
ROOT = "/workspace/data"
LABEL = "tiny-hermes.test"


@dataclass(frozen=True)
class Executed:
    exit_code: int
    stdout: bytes
    stderr: bytes


@pytest.fixture
async def box(docker_client: Any, image_digest: str) -> Any:
    """One runtime container with a writable data root, swept by conftest."""

    def start() -> Any:
        return docker_client.containers.run(
            image_digest,
            command=["sleep", "infinity"],
            detach=True,
            user="10001:10001",
            labels={LABEL: "1"},
            tmpfs={ROOT: "rw,uid=10001,gid=10001"},
        )

    return await asyncio.to_thread(start)


async def _exec(box: Any, argv: list[str] | str) -> Executed:
    def run() -> Executed:
        code, (out, err) = box.exec_run(argv, demux=True, workdir=ROOT)
        return Executed(int(code), out or b"", err or b"")

    return await asyncio.to_thread(run)


async def _helper(box: Any, *args: str) -> Executed:
    return await _exec(box, [HELPER, "--root", ROOT, *args])


async def _write(box: Any, rel: str, content: str, limit: int = 1024) -> Executed:
    command = (
        f"printf %s '{content}' | {HELPER} --root {ROOT} write '{rel}' {limit}"
    )
    return await _exec(box, ["sh", "-c", command])


async def test_helper_probe_reports_openat2_support(box: Any) -> None:
    probe = await _helper(box, "probe")
    assert probe.exit_code == 0, probe.stderr


async def test_helper_reads_writes_and_lists_beneath_data_root(box: Any) -> None:
    written = await _write(box, "notes/a.txt", "hello")
    assert written.exit_code == 0, written.stderr

    read = await _helper(box, "read", "notes/a.txt", "1024")
    assert read.exit_code == 0
    assert read.stdout == b"hello"

    listing = json.loads((await _helper(box, "list", "notes", "0", "100")).stdout)
    assert [entry["path"] for entry in listing["entries"]] == ["a.txt"]
    assert listing["entries"][0]["type"] == "file"
    assert listing["entries"][0]["size"] == len(b"hello")


async def test_list_is_bytewise_ordered_and_paginated(box: Any) -> None:
    for name in ("b.txt", "a.txt", "z.txt", "c.txt"):
        assert (await _write(box, f"many/{name}", "x")).exit_code == 0

    page = json.loads((await _helper(box, "list", "many", "1", "2")).stdout)
    assert [entry["path"] for entry in page["entries"]] == ["b.txt", "c.txt"]
    assert page["total"] == 4

    not_a_directory = await _helper(box, "list", "many/a.txt", "0", "10")
    assert not_a_directory.exit_code != 0


async def test_paths_that_leave_the_root_die_before_any_syscall(box: Any) -> None:
    for hostile in ("../escape", "/etc/hostname", "a/../../escape", ""):
        for verb, extra in (("read", ["4096"]), ("list", ["0", "10"])):
            refused = await _helper(box, verb, hostile, *extra)
            assert refused.exit_code != 0, f"{verb} accepted {hostile!r}"


async def test_a_symlink_in_the_path_is_refused_by_the_kernel(box: Any) -> None:
    assert (await _write(box, "real/hostname", "safe")).exit_code == 0
    planted = await _exec(box, ["ln", "-s", "/etc", f"{ROOT}/evil"])
    assert planted.exit_code == 0

    through = await _helper(box, "read", "evil/hostname", "4096")
    assert through.exit_code != 0
    assert b"passwd" not in through.stdout

    link_file = await _exec(
        box, ["ln", "-s", "/etc/hostname", f"{ROOT}/real/link"]
    )
    assert link_file.exit_code == 0
    direct = await _helper(box, "read", "real/link", "4096")
    assert direct.exit_code != 0


async def test_helper_refuses_symlink_traversal_even_when_racing(box: Any) -> None:
    """A directory that flickers into a symlink mid-walk must never win.

    A background loop swaps `dir` between a real directory (whose `hostname`
    says "safe") and a symlink to /etc, while the helper reads `dir/hostname`
    in a loop. openat2's RESOLVE_NO_SYMLINKS makes the check and the use one
    syscall: every successful read must have come from the real directory.
    """
    assert (await _write(box, "real/hostname", "safe")).exit_code == 0
    etc_hostname = (await _exec(box, ["cat", "/etc/hostname"])).stdout

    # The real directory is staged complete and swapped in with one rename, so
    # a mid-swap read sees the symlink, nothing, or the whole file — a partial
    # copy would count as a leak it is not.
    swapper = (
        "i=0; while [ $i -lt 400 ]; do "
        f"rm -rf {ROOT}/dir {ROOT}/dir.tmp; ln -s /etc {ROOT}/dir; "
        f"mkdir {ROOT}/dir.tmp; printf %s safe > {ROOT}/dir.tmp/hostname; "
        f"rm -f {ROOT}/dir; mv -T {ROOT}/dir.tmp {ROOT}/dir; "
        "i=$((i+1)); done"
    )
    reader = (
        "wins=0; leaks=0; i=0; while [ $i -lt 800 ]; do "
        f"out=$({HELPER} --root {ROOT} read dir/hostname 64 2>/dev/null); "
        'if [ $? -eq 0 ]; then case "$out" in safe) wins=$((wins+1));; "") ;; '
        "*) leaks=$((leaks+1));; esac; fi; "
        "i=$((i+1)); done; echo $wins $leaks"
    )

    swap_task = asyncio.create_task(_exec(box, ["sh", "-c", swapper]))
    read_result = await _exec(box, ["sh", "-c", reader])
    await swap_task

    wins, leaks = (int(v) for v in read_result.stdout.split())
    assert leaks == 0, f"{leaks} reads returned {etc_hostname!r} through the symlink"
    assert wins > 0, "the loop never observed the real directory; the race proved nothing"


async def test_write_is_atomic_same_directory_tmp_plus_rename(box: Any) -> None:
    assert (await _write(box, "doc/report.txt", "first")).exit_code == 0
    assert (await _write(box, "doc/report.txt", "second")).exit_code == 0

    read = await _helper(box, "read", "doc/report.txt", "1024")
    assert read.stdout == b"second"

    listing = json.loads((await _helper(box, "list", "doc", "0", "100")).stdout)
    names = [entry["path"] for entry in listing["entries"]]
    assert names == ["report.txt"], f"temporary files leaked: {names}"


async def test_read_beyond_its_limit_reports_truncation_honestly(box: Any) -> None:
    assert (await _write(box, "big.txt", "0123456789")).exit_code == 0
    truncated = await _helper(box, "read", "big.txt", "4")
    assert truncated.exit_code == 3
    assert truncated.stdout == b"0123"
    assert json.loads(truncated.stderr)["truncated"] is True


async def test_write_beyond_its_limit_leaves_no_partial_file(box: Any) -> None:
    over = await _write(box, "capped.txt", "0123456789", limit=4)
    assert over.exit_code != 0
    missing = await _helper(box, "read", "capped.txt", "64")
    assert missing.exit_code != 0, "a refused write must not leave bytes behind"
    listing = json.loads((await _helper(box, "list", ".", "0", "100")).stdout)
    assert all(not e["path"].startswith(".tmp-") for e in listing["entries"])


async def test_read_of_a_missing_file_is_an_error_not_empty_output(box: Any) -> None:
    missing = await _helper(box, "read", "never/was.txt", "64")
    assert missing.exit_code not in (0, 3)
    assert missing.stdout == b""
