"""The bound sandbox port: frames and tars in, domain values out.

The demux is the part worth testing without Docker: a daemon-produced tar
arrives as callback chunks, and `export_files` must hand each requested body
to the service in order, whole, and without ever extracting to a filesystem.
"""

import io
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.session_workspace.infrastructure.sandbox_port import (
    ControllerWorkspacePort,
)

RUN = uuid4()
LEASE = uuid4()
SANDBOX = uuid4()


def _tar_of(files: dict[str, bytes]) -> bytes:
    """A tar the way `get_archive("/workspace/data")` shapes one: rooted."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        root = tarfile.TarInfo("data")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for path, body in files.items():
            info = tarfile.TarInfo(f"data/{path}")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


class FakeGateway:
    """Answers the three workspace calls with scripted data."""

    def __init__(self) -> None:
        self.tar = b""
        self.scan_answer: tuple[Any, ...] = ()
        self.imported = bytearray()
        self.calls: list[tuple[str, UUID, UUID, UUID]] = []

    async def workspace_scan(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> tuple[Any, ...]:
        self.calls.append(("scan", run_id, lease_id, sandbox_id))
        return self.scan_answer

    async def workspace_import(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        tar_stream: AsyncIterator[bytes],
        declared_total: int,
    ) -> dict[str, Any]:
        del declared_total
        self.calls.append(("import", run_id, lease_id, sandbox_id))
        async for chunk in tar_stream:
            self.imported.extend(chunk)
        return {"received_bytes": len(self.imported)}

    async def workspace_export(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        sink: Callable[[bytes], Awaitable[None]],
        limit: int,
    ) -> dict[str, Any]:
        del limit
        self.calls.append(("export", run_id, lease_id, sandbox_id))
        for start in range(0, len(self.tar), 1000):  # deliberately odd chunking
            await sink(self.tar[start : start + 1000])
        return {"total_bytes": len(self.tar)}


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def port(gateway: FakeGateway) -> ControllerWorkspacePort:
    return ControllerWorkspacePort(
        gateway=gateway,
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=SANDBOX,
        export_limit=10 * 1024 * 1024,
    )


async def _collect(port: ControllerWorkspacePort, paths: Sequence[str]) -> dict[str, bytes]:
    received: dict[str, bytes] = {}
    async for exported in port.export_files(paths):
        body = bytearray()
        async for chunk in exported.chunks:
            body.extend(chunk)
        received[exported.path] = bytes(body)
    return received


async def test_export_files_demuxes_only_the_requested_bodies(
    port: ControllerWorkspacePort, gateway: FakeGateway
) -> None:
    files = {
        "a.txt": b"alpha",
        "big.bin": bytes(range(256)) * 3000,
        "skip.txt": b"not asked for",
    }
    gateway.tar = _tar_of(files)

    received = await _collect(port, ["a.txt", "big.bin"])

    assert received == {"a.txt": files["a.txt"], "big.bin": files["big.bin"]}


async def test_export_files_of_nothing_asks_the_sandbox_for_nothing(
    port: ControllerWorkspacePort, gateway: FakeGateway
) -> None:
    received = await _collect(port, [])
    assert received == {}
    assert gateway.calls == [], "an empty request must not stream a whole tree"


async def test_import_tree_passes_the_stream_through_bound_ids(
    port: ControllerWorkspacePort, gateway: FakeGateway
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"tar "
        yield b"bytes"

    await port.import_tree(chunks(), declared_total=9)

    assert bytes(gateway.imported) == b"tar bytes"
    assert gateway.calls == [("import", RUN, LEASE, SANDBOX)]


async def test_scan_translates_to_domain_entries(
    port: ControllerWorkspacePort, gateway: FakeGateway
) -> None:
    from tiny_hermes.sandbox.domain.command import ScannedEntry  # noqa: PLC0415

    gateway.scan_answer = (
        ScannedEntry(path="x.txt", entry_type="file", mode=0o644, size=1, sha256="f" * 64),
    )

    entries = await port.scan()

    assert entries[0].path == "x.txt"
    assert entries[0].entry_type == "file"
    assert entries[0].sha256 == "f" * 64
