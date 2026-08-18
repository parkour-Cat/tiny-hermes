"""The only module in this process that speaks to a Docker daemon.

docker-py is synchronous, so every call goes through `asyncio.to_thread`. That
is a deliberate choice over an async client: docker-py speaks the Windows named
pipe and the async alternatives do not, and this repository is developed on
Windows. A thread per Docker call is not a cost worth optimizing when the calls
themselves take tens of milliseconds and a container start takes hundreds.

This module holds no rules. It creates what it is told to create; deciding what
may be created is `domain/container_policy.py`, and deciding who may ask is
`application/controller.py`.
"""

import asyncio
import hashlib
import tarfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, cast

from tiny_hermes.sandbox.domain.command import (
    CommandResult,
    OutputSink,
    SandboxCommand,
    ScannedEntry,
    StreamedResult,
)
from tiny_hermes.sandbox.domain.container_policy import ContainerConfig

_SCAN_CHUNK = 1024 * 1024


class DockerUnavailable(Exception):
    """The daemon could not be reached, or refused.

    Distinct from a refusal: a refusal is the platform saying no, and this is
    the platform being unable to ask.
    """


@dataclass(frozen=True)
class VolumeInfo:
    name: str
    labels: dict[str, str]


@dataclass
class DockerEngine:
    client: Any
    #: Added to every container this engine creates. The test suite uses it to
    #: sweep; a deployment can use it to find what a crashed Controller left.
    extra_labels: dict[str, str] | None = None

    async def create(self, config: ContainerConfig) -> str:
        kwargs = config.as_docker_kwargs()
        if self.extra_labels:
            kwargs["labels"] = {**kwargs["labels"], **self.extra_labels}
        container = await self._call(self.client.containers.run, **kwargs)
        return str(container.id)

    async def execute(self, container_id: str, command: SandboxCommand) -> CommandResult:
        container = await self._call(self.client.containers.get, container_id)
        handle = await self._call(
            self.client.api.exec_create,
            container.id,
            cmd=command.argv,
            workdir=command.cwd,
            stdout=True,
            stderr=True,
            stdin=command.stdin is not None,
            # No tty: a tty merges streams through a pty and rewrites newlines,
            # which turns exact output into approximately-right output.
            tty=False,
        )
        try:
            if command.stdin is None:
                raw: bytes = await asyncio.wait_for(
                    self._call(self.client.api.exec_start, handle["Id"], demux=False),
                    timeout=command.timeout_seconds,
                )
            else:
                raw = await asyncio.wait_for(
                    self._call(self._exec_with_stdin, handle["Id"], command.stdin),
                    timeout=command.timeout_seconds,
                )
        except TimeoutError:
            # The exec is still running inside the container. It will be
            # collected when the container is destroyed, and the Run is told
            # what happened rather than left waiting.
            return CommandResult(exit_code=124, output="", truncated=False, timed_out=True)

        truncated = len(raw) > command.output_limit
        body = raw[: command.output_limit]
        if truncated:
            body += b"\n[output truncated by the platform]"
        info = await self._call(self.client.api.exec_inspect, handle["Id"])
        return CommandResult(
            exit_code=int(info.get("ExitCode") or 0),
            output=body.decode("utf-8", errors="replace"),
            truncated=truncated,
            timed_out=False,
        )

    # -- volumes -------------------------------------------------------------

    async def address_of(self, container_id: str) -> str | None:
        """Where this container's packets will come from, or None.

        `None` for a container on no network, which has no address and needs no
        identity: it cannot reach the proxy, so nobody will ever ask who it is.

        Read back from Docker rather than assumed, because the address is
        Docker's to hand out and the proxy compares against the one that
        actually arrives.
        """
        container = await self._call(self.client.containers.get, container_id)
        settings: Any = container.attrs.get("NetworkSettings", {})
        networks: Any = settings.get("Networks", {})
        for attached in networks.values():
            address = attached.get("IPAddress")
            if address:
                return str(address)
        direct = settings.get("IPAddress")
        return str(direct) if direct else None

    async def create_volume(self, name: str, labels: dict[str, str]) -> str:
        volume = await self._call(self.client.volumes.create, name=name, labels=labels)
        return str(volume.name)

    async def remove_volume(self, name: str) -> None:
        volume = await self._call(self.client.volumes.get, name)
        await self._call(volume.remove, force=True)

    async def volumes_labelled(self, label: str) -> list[VolumeInfo]:
        """Every volume carrying the label — by label, never by parsed name."""
        found: list[Any] = await self._call(
            self.client.volumes.list, filters={"label": label}
        )
        infos: list[VolumeInfo] = []
        for volume in found:
            attrs = cast(dict[str, Any], volume.attrs or {})
            labels = cast(dict[str, str], attrs.get("Labels") or {})
            infos.append(VolumeInfo(name=str(volume.name), labels=dict(labels)))
        return infos

    # -- workspace trees -------------------------------------------------------

    async def import_tree(
        self, container_id: str, target: str, tar_stream: AsyncIterator[bytes]
    ) -> None:
        """Hand a tar stream to the daemon for extraction inside the container.

        The daemon extracts; this process never does. What the stream may
        contain was decided by the caller before the first byte got here.
        """
        container = await self._call(self.client.containers.get, container_id)
        loop = asyncio.get_running_loop()

        def upload() -> None:
            container.put_archive(target, _pulled_from(loop, tar_stream))

        await self._call(upload)

    async def export_tree(
        self, container_id: str, source: str
    ) -> AsyncIterator[bytes]:
        container = await self._call(self.client.containers.get, container_id)

        def open_archive() -> Iterator[bytes]:
            stream, _ = container.get_archive(source, chunk_size=_SCAN_CHUNK)
            return iter(stream)

        chunks = await self._call(open_archive)
        sentinel = object()
        while True:
            chunk = await self._call(next, chunks, sentinel)
            if chunk is sentinel:
                return
            yield bytes(chunk)

    async def scan_tree(
        self, container_id: str, source: str
    ) -> tuple[ScannedEntry, ...]:
        """Metadata and digests from the export stream, extracted nowhere.

        Reads the tar with ``tarfile.open(mode="r|")`` and hashes file bodies
        as they pass. It never calls ``extract*`` and never touches the host
        filesystem — the architecture test bans the alternative outright.
        """
        container = await self._call(self.client.containers.get, container_id)

        def scan() -> tuple[ScannedEntry, ...]:
            stream, _ = container.get_archive(source, chunk_size=_SCAN_CHUNK)
            reader = _IteratorFile(iter(stream))
            entries: list[ScannedEntry] = []
            with tarfile.open(fileobj=cast(Any, reader), mode="r|") as archive:  # noqa: S202 - stream iteration only, never extract*
                for member in archive:
                    path = _relative_name(member.name)
                    if not path:
                        continue
                    digest: str | None = None
                    size = 0
                    if member.isreg():
                        hasher = hashlib.sha256()
                        body = archive.extractfile(member)
                        if body is not None:
                            while True:
                                piece = body.read(_SCAN_CHUNK)
                                if not piece:
                                    break
                                hasher.update(piece)
                        digest = hasher.hexdigest()
                        size = int(member.size)
                    entries.append(
                        ScannedEntry(
                            path=path,
                            entry_type=_member_type(member),
                            mode=int(member.mode),
                            size=size,
                            sha256=digest,
                        )
                    )
            return tuple(entries)

        return await self._call(scan)

    async def execute_streamed(
        self, container_id: str, command: SandboxCommand, sink: OutputSink
    ) -> StreamedResult:
        """Run a command and drain its output past every cap.

        At most ``sink.artifact_limit`` bytes reach the sink; everything after
        is read and discarded so a noisy child can always finish writing
        instead of blocking on a full pipe (design §11).
        """
        container = await self._call(self.client.containers.get, container_id)
        handle = await self._call(
            self.client.api.exec_create,
            container.id,
            cmd=command.argv,
            workdir=command.cwd,
            stdout=True,
            stderr=True,
            tty=False,
        )
        loop = asyncio.get_running_loop()
        limit = sink.artifact_limit
        observed = 0
        delivered = 0

        def drain() -> None:
            nonlocal observed, delivered
            stream = self.client.api.exec_start(handle["Id"], stream=True, demux=False)
            for chunk in stream:
                observed += len(chunk)
                room = limit - delivered
                if room > 0:
                    portion = bytes(chunk[:room])
                    # Waited on, deliberately: the sink's pace is the
                    # backpressure. Only bytes past the cap are discarded
                    # without waiting for anyone.
                    asyncio.run_coroutine_threadsafe(
                        sink.deliver(portion), loop
                    ).result()
                    delivered += len(portion)

        timed_out = False
        try:
            await asyncio.wait_for(self._call(drain), timeout=command.timeout_seconds)
        except TimeoutError:
            # The exec is still running inside the container and is collected
            # when the container is destroyed; the Run is told what happened.
            timed_out = True

        exit_code = 124
        if not timed_out:
            info = await self._call(self.client.api.exec_inspect, handle["Id"])
            exit_code = int(info.get("ExitCode") or 0)
        return StreamedResult(
            exit_code=exit_code,
            timed_out=timed_out,
            observed_bytes=observed,
            delivered_bytes=delivered,
            truncated=observed > delivered,
        )

    async def pause(self, container_id: str) -> None:
        container = await self._call(self.client.containers.get, container_id)
        await self._call(container.pause)

    async def unpause(self, container_id: str) -> None:
        container = await self._call(self.client.containers.get, container_id)
        await self._call(container.unpause)

    async def remove(self, container_id: str) -> None:
        container = await self._call(self.client.containers.get, container_id)
        await self._call(container.remove, force=True)

    def _exec_with_stdin(self, exec_id: str, stdin: bytes) -> bytes:
        """Feed stdin whole, close it, then collect the demuxed output.

        Runs in the engine's worker thread. Written for the file helper, which
        reads all of stdin before it produces output — a command that streams
        output while its input is still arriving could fill the daemon's
        buffer, and no M1 tool does that.
        """
        import docker.utils.socket as docker_socket  # noqa: PLC0415

        untyped: Any = docker_socket
        sock = self.client.api.exec_start(exec_id, socket=True, demux=False)
        received = bytearray()
        with sock:
            inner: Any = getattr(sock, "_sock", sock)
            inner.sendall(stdin)
            import socket as stdlib_socket  # noqa: PLC0415

            inner.shutdown(stdlib_socket.SHUT_WR)
            for frame in cast(
                list[tuple[int, bytes]], untyped.frames_iter(sock, tty=False)
            ):
                received.extend(frame[1])
        return bytes(received)

    async def _call(self, work: Any, *args: Any, **kwargs: Any) -> Any:
        from docker.errors import DockerException  # noqa: PLC0415 - narrow the import

        try:
            return await asyncio.to_thread(lambda: work(*args, **kwargs))
        except DockerException as failure:
            raise DockerUnavailable(str(failure)) from failure


def _relative_name(raw: str) -> str:
    """The member's path with the archive's root component removed.

    `get_archive("/workspace/data")` wraps everything in a `data/` prefix; the
    root entry itself becomes empty and is skipped by the caller. No other
    normalization happens here — judging paths is the domain's job.
    """
    _, _, rest = raw.partition("/")
    return rest.rstrip("/")


def _member_type(member: tarfile.TarInfo) -> str:
    if member.isreg():
        return "file"
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr() or member.isblk():
        return "device"
    if member.isfifo():
        return "fifo"
    return "other"


def _pulled_from(
    loop: asyncio.AbstractEventLoop, source: AsyncIterator[bytes]
) -> Iterator[bytes]:
    """A sync iterator the SDK can consume, fed by the caller's async one.

    Runs inside the upload thread; each pull asks the event loop for the next
    chunk, so at no point does the whole tree sit in this process's memory.
    """
    while True:
        chunk = asyncio.run_coroutine_threadsafe(anext(source, None), loop).result()
        if chunk is None:
            return
        yield chunk


class _IteratorFile:
    """The minimal file-like face `tarfile` needs over a chunk iterator."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._pending = b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._pending) < size:
            piece = next(self._chunks, None)
            if piece is None:
                break
            self._pending += piece
        if size < 0:
            answer, self._pending = self._pending, b""
            return answer
        answer, self._pending = self._pending[:size], self._pending[size:]
        return answer
