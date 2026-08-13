"""The SandboxWorkspacePort over the controller gateway, bound to one slice.

The interesting half is the export demux: the Controller exports the data
tree as one tar stream, and the service wants named file bodies. The tar is
parsed with `tarfile` in a worker thread fed chunk by chunk from the event
loop — never extracted, never written to any filesystem — and each requested
body flows back as an async iterator the service can hand straight to the
object store.
"""

import asyncio
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol, cast
from uuid import UUID

from tiny_hermes.session_workspace.ports.sandbox import ExportedFile, ScanEntry

_PARSE_CHUNK = 1024 * 1024
#: Queue depths: small, because the queues exist for handoff, not buffering.
_DEPTH = 8


class _ScannedLike(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def entry_type(self) -> str: ...
    @property
    def mode(self) -> int: ...
    @property
    def size(self) -> int: ...
    @property
    def sha256(self) -> str | None: ...


class WorkspaceGateway(Protocol):
    """The three workspace calls, as the transport adapter exposes them."""

    async def workspace_scan(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> tuple[_ScannedLike, ...]: ...

    async def workspace_import(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        tar_stream: AsyncIterator[bytes],
        declared_total: int,
    ) -> dict[str, Any]: ...

    async def workspace_export(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        sink: Callable[[bytes], Awaitable[None]],
        limit: int,
    ) -> dict[str, Any]: ...


class ControllerWorkspacePort:
    """A port instance is one slice's: the ids are fixed at construction."""

    def __init__(
        self,
        *,
        gateway: WorkspaceGateway,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        export_limit: int,
    ) -> None:
        self._gateway = gateway
        self._run_id = run_id
        self._lease_id = lease_id
        self._sandbox_id = sandbox_id
        self._export_limit = export_limit

    async def scan(self) -> tuple[ScanEntry, ...]:
        scanned = await self._gateway.workspace_scan(
            run_id=self._run_id, lease_id=self._lease_id, sandbox_id=self._sandbox_id
        )
        return tuple(
            ScanEntry(
                path=entry.path,
                entry_type=entry.entry_type,
                mode=entry.mode,
                size=entry.size,
                sha256=entry.sha256,
            )
            for entry in scanned
        )

    async def import_tree(
        self, tar_stream: AsyncIterator[bytes], *, declared_total: int
    ) -> None:
        await self._gateway.workspace_import(
            run_id=self._run_id,
            lease_id=self._lease_id,
            sandbox_id=self._sandbox_id,
            tar_stream=tar_stream,
            declared_total=declared_total,
        )

    async def export_files(self, paths: Sequence[str]) -> AsyncIterator[ExportedFile]:
        wanted = set(paths)
        if not wanted:
            # No byte owed, no byte moved.
            return

        loop = asyncio.get_running_loop()
        raw: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_DEPTH)
        events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=_DEPTH)

        async def receive(chunk: bytes) -> None:
            await raw.put(chunk)

        async def pump() -> None:
            try:
                await self._gateway.workspace_export(
                    run_id=self._run_id,
                    lease_id=self._lease_id,
                    sandbox_id=self._sandbox_id,
                    sink=receive,
                    limit=self._export_limit,
                )
            finally:
                await raw.put(None)

        def emit(event: tuple[str, Any]) -> None:
            asyncio.run_coroutine_threadsafe(events.put(event), loop).result()

        def parse() -> None:
            try:
                reader = _LoopFedFile(loop, raw)
                with tarfile.open(fileobj=cast(Any, reader), mode="r|") as archive:  # noqa: S202 - streamed, never extract*
                    for member in archive:
                        path = member.name.partition("/")[2]
                        if not member.isreg() or path not in wanted:
                            continue
                        emit(("file", (path, int(member.size))))
                        body = archive.extractfile(member)
                        while body is not None:
                            piece = body.read(_PARSE_CHUNK)
                            if not piece:
                                break
                            emit(("chunk", piece))
                        emit(("end", None))
                emit(("done", None))
            except BaseException as broken:  # noqa: BLE001 - reported to the loop
                emit(("broken", broken))

        exporter = asyncio.create_task(pump())
        parser = asyncio.create_task(asyncio.to_thread(parse))
        try:
            while True:
                kind, payload = await events.get()
                if kind == "done":
                    return
                if kind == "broken":
                    raise cast(BaseException, payload)
                path, size = cast(tuple[str, int], payload)
                yield ExportedFile(
                    path=path, size=size, chunks=_body_of(events)
                )
        finally:
            await asyncio.gather(exporter, parser, return_exceptions=True)


async def _body_of(events: asyncio.Queue[tuple[str, Any]]) -> AsyncIterator[bytes]:
    """One file's chunks, ending at its `end` marker.

    The service consumes each file completely before asking for the next, so
    a shared queue with markers is a handoff, not a protocol.
    """
    while True:
        kind, payload = await events.get()
        if kind == "end":
            return
        if kind == "broken":
            raise cast(BaseException, payload)
        yield cast(bytes, payload)


class _LoopFedFile:
    """The blocking file `tarfile` wants, fed by the event loop's queue."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, source: asyncio.Queue[bytes | None]
    ) -> None:
        self._loop = loop
        self._source = source
        self._pending = b""
        self._finished = False

    def read(self, size: int = -1) -> bytes:
        while not self._finished and (size < 0 or len(self._pending) < size):
            chunk = asyncio.run_coroutine_threadsafe(
                self._source.get(), self._loop
            ).result()
            if chunk is None:
                self._finished = True
                break
            self._pending += chunk
        if size < 0:
            answer, self._pending = self._pending, b""
            return answer
        answer, self._pending = self._pending[:size], self._pending[size:]
        return answer
