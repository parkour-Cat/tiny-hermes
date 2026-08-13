"""The engine's workspace mechanics against a real daemon.

Volume lifecycle by label, tar import/export against a paused container, a
scan that hashes from the stream without ever extracting to the host
filesystem, streamed exec that drains a noisy child past its caps, and the
cache tmpfs ceilings design §16.3 wants proven by the kernel rather than
promised by configuration.
"""

import asyncio
import hashlib
import io
import tarfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import (
    DEFAULT_PROFILE,
    container_config,
)
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine

LABEL = "tiny-hermes.test"
DATA = "/workspace/data"


@pytest.fixture
def engine(docker_client: Any) -> DockerEngine:
    return DockerEngine(docker_client, extra_labels={LABEL: "1"})


@pytest.fixture
async def box(docker_client: Any, image_digest: str) -> Any:
    """A running container with a real named data volume, swept by conftest."""
    name = f"tiny-hermes-data-{uuid.uuid4()}"

    def start() -> Any:
        docker_client.volumes.create(name=name)
        return docker_client.containers.run(
            image_digest,
            command=["sleep", "infinity"],
            detach=True,
            user="10001:10001",
            labels={LABEL: "1"},
            volumes={name: {"bind": DATA, "mode": "rw"}},
        )

    return await asyncio.to_thread(start)


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directories: set[str] = set()
        for path in files:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent and parent not in directories:
                directories.add(parent)
                info = tarfile.TarInfo(parent)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
        for path, body in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


async def _chunks(data: bytes, size: int = 65536) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


@dataclass
class CollectingSink:
    """Keeps what the engine delivers, up to its declared ceiling."""

    artifact_limit: int
    received: bytearray = field(default_factory=bytearray)

    async def deliver(self, chunk: bytes) -> None:
        self.received.extend(chunk)


async def test_volume_lifecycle_with_labels(engine: DockerEngine) -> None:
    run_id = uuid.uuid4()
    name = f"tiny-hermes-data-{run_id}"
    labels = {"tiny-hermes.run": str(run_id), "tiny-hermes.workspace": "w"}

    created = await engine.create_volume(name, labels)
    assert created == name

    found = await engine.volumes_labelled("tiny-hermes.run")
    match = [volume for volume in found if volume.name == name]
    assert match, "the created volume must be discoverable by label"
    assert match[0].labels["tiny-hermes.run"] == str(run_id)

    await engine.remove_volume(name)
    assert name not in [v.name for v in await engine.volumes_labelled("tiny-hermes.run")]


async def test_import_then_scan_round_trips_paths_sizes_and_digests(
    engine: DockerEngine, box: Any
) -> None:
    files = {"a.txt": b"alpha", "nested/b.bin": b"\x00\x01\x02" * 100}
    await engine.pause(box.id)
    await engine.import_tree(box.id, DATA, _chunks(_tar_bytes(files)))

    scanned = await engine.scan_tree(box.id, DATA)

    by_path = {entry.path: entry for entry in scanned}
    assert by_path["a.txt"].entry_type == "file"
    assert by_path["a.txt"].size == len(b"alpha")
    assert by_path["a.txt"].sha256 == hashlib.sha256(b"alpha").hexdigest()
    assert by_path["nested"].entry_type == "directory"
    assert by_path["nested/b.bin"].sha256 == hashlib.sha256(b"\x00\x01\x02" * 100).hexdigest()


async def test_export_streams_back_what_was_imported(
    engine: DockerEngine, box: Any
) -> None:
    files = {"keep/me.txt": b"exported"}
    await engine.pause(box.id)
    await engine.import_tree(box.id, DATA, _chunks(_tar_bytes(files)))

    received = bytearray()
    async for chunk in engine.export_tree(box.id, DATA):
        received.extend(chunk)

    names: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(bytes(received)), mode="r|") as archive:
        names = [member.name for member in archive]
    assert any(name.endswith("keep/me.txt") for name in names)


async def test_scan_reports_special_entries_rather_than_skipping_them(
    engine: DockerEngine, box: Any
) -> None:
    planted = await asyncio.to_thread(
        box.exec_run, ["ln", "-s", "/etc/passwd", f"{DATA}/sneaky"]
    )
    assert planted[0] == 0
    await engine.pause(box.id)

    scanned = await engine.scan_tree(box.id, DATA)

    types = {entry.path: entry.entry_type for entry in scanned}
    assert types["sneaky"] == "symlink", "the scanner reports; the caller refuses"


async def test_exec_stream_drains_beyond_the_cap_and_reports_totals(
    engine: DockerEngine, box: Any
) -> None:
    total = 8 * 1024 * 1024
    cap = 1024 * 1024
    sink = CollectingSink(artifact_limit=cap)
    command = SandboxCommand(
        argv=["sh", "-c", f"yes | head -c {total}"],
        cwd=DATA,
        timeout_seconds=60,
        output_limit=cap,
    )

    result = await engine.execute_streamed(box.id, command, sink)

    assert result.exit_code == 0, "nothing may block on a full pipe"
    assert not result.timed_out
    assert result.observed_bytes == total
    assert result.delivered_bytes == cap
    assert len(sink.received) == cap
    assert result.truncated


async def test_execute_feeds_stdin_and_the_helper_writes_atomically(
    engine: DockerEngine, box: Any
) -> None:
    """file.write's whole delivery path: stdin to helper to rename, no shell."""
    helper = "/usr/local/bin/tiny-hermes-file-helper"
    body = b"stdin travels whole\n" * 1000
    wrote = await engine.execute(
        box.id,
        SandboxCommand(
            argv=[helper, "--root", DATA, "write", "notes/in.txt", str(len(body))],
            cwd=DATA,
            timeout_seconds=30,
            output_limit=4096,
            stdin=body,
        ),
    )
    assert wrote.exit_code == 0, wrote.output

    read = await engine.execute(
        box.id,
        SandboxCommand(
            argv=[helper, "--root", DATA, "read", "notes/in.txt", str(len(body))],
            cwd=DATA,
            timeout_seconds=30,
            output_limit=len(body) + 4096,
        ),
    )
    assert read.exit_code == 0
    assert read.output == body.decode()


async def test_cache_tmpfs_returns_enospc_at_size_and_inode_ceilings(
    docker_client: Any, image_digest: str
) -> None:
    """Design §16.3: the ceilings are the kernel's answer, not a policy's."""
    tiny = replace(DEFAULT_PROFILE, cache_mb=1, cache_inodes=16)
    config = container_config(
        digest=image_digest,
        profile=tiny,
        run_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        approved_digests=(image_digest,),
        ceiling=tiny,
    )
    kwargs = config.as_docker_kwargs()
    kwargs["labels"] = {**kwargs["labels"], LABEL: "1"}
    box = await asyncio.to_thread(lambda: docker_client.containers.run(**kwargs))

    def run(command: str) -> tuple[int, bytes]:
        code, output = box.exec_run(["sh", "-c", command])
        return int(code), output

    over_bytes = await asyncio.to_thread(
        run, "dd if=/dev/zero of=/workspace/cache/fill bs=1024 count=1025 2>&1"
    )
    assert over_bytes[0] != 0
    assert b"space" in over_bytes[1].lower(), over_bytes[1]

    over_inodes = await asyncio.to_thread(
        run,
        "rm -f /workspace/cache/fill; i=0; while [ $i -lt 32 ]; do "
        "touch /workspace/cache/f$i 2>/dev/null || exit 28; i=$((i+1)); done; exit 0",
    )
    assert over_inodes[0] == 28, "the seventeenth inode must be refused"

    data_ok = await asyncio.to_thread(
        run, "dd if=/dev/zero of=/workspace/data/fill bs=1024 count=2048 2>&1 && echo ok"
    )
    assert data_ok[0] == 0, "the data volume answers to the checkpoint quota, not tmpfs"


async def test_cache_survives_freeze_thaw_and_dies_with_the_instance(
    engine: DockerEngine, docker_client: Any, image_digest: str
) -> None:
    config = container_config(
        digest=image_digest,
        profile=DEFAULT_PROFILE,
        run_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        approved_digests=(image_digest,),
    )
    kwargs = config.as_docker_kwargs()
    kwargs["labels"] = {**kwargs["labels"], LABEL: "1"}
    box = await asyncio.to_thread(lambda: docker_client.containers.run(**kwargs))

    wrote = await asyncio.to_thread(
        box.exec_run, ["sh", "-c", "echo warm > /workspace/cache/state"]
    )
    assert wrote[0] == 0
    await engine.pause(box.id)
    await engine.unpause(box.id)
    still = await asyncio.to_thread(box.exec_run, ["cat", "/workspace/cache/state"])
    assert still[0] == 0 and still[1].strip() == b"warm"

    await engine.remove(box.id)
    successor = await asyncio.to_thread(
        lambda: docker_client.containers.run(**kwargs)
    )
    gone = await asyncio.to_thread(successor.exec_run, ["cat", "/workspace/cache/state"])
    assert gone[0] != 0, "cache must not outlive the instance it warmed"
