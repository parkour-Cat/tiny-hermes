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
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from time import monotonic
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

#: Between the TERM `timeout` sends at the deadline and the KILL that follows
#: for whatever ignored it.
_KILL_AFTER_SECONDS = 2
#: How long past the deadline the controller stops reading on its own, for an
#: exec that `timeout` somehow did not end. Its thread is then lost until the
#: container goes, which is what every timeout cost before `timeout` was used.
_BACKSTOP_SECONDS = _KILL_AFTER_SECONDS + 5


def _bounded(command: SandboxCommand) -> list[str]:
    """The command under coreutils `timeout`, inside the container.

    The controller cannot kill an exec: the daemon has no call for it, and its
    pid is in a namespace the controller does not see. `timeout` makes itself
    a process-group leader and signals the whole group at the deadline, so a
    `sleep &` the command started goes with it. A child that left the group
    (`setsid`, a daemonizing server) does not, and still lives until the
    container is destroyed.

    So every approved image must ship `timeout`; the platform's own does, as
    part of coreutils.
    """
    return [
        "timeout",
        f"--kill-after={_KILL_AFTER_SECONDS}",
        str(command.timeout_seconds),
        *command.argv,
    ]


def _ran_out(exit_code: int, elapsed: float, command: SandboxCommand) -> bool:
    """124 is `timeout`'s own answer; 137 is its KILL, unless the command was
    killed some other way (the OOM killer) before its deadline."""
    return exit_code == 124 or (exit_code == 137 and elapsed >= command.timeout_seconds)


class DockerUnavailable(Exception):
    """The daemon could not be reached, or refused.

    Distinct from a refusal: a refusal is the platform saying no, and this is
    the platform being unable to ask.
    """


class ContainerGone(DockerUnavailable):
    """The daemon answered: no such container.

    Removed outside the platform, or by a sweep that should not have, as on
    2026-10-24. A subclass for the reason `ContainerNotRunning` gives.
    """


class ContainerNotRunning(DockerUnavailable):
    """The daemon answered: the container is there and has stopped.

    A subclass so every caller that treats an engine failure as "the sandbox
    cannot be vouched for" still does. Only the name changes, because the
    daemon was reachable and an operator who reads "DockerUnavailable" goes
    looking at the wrong thing.
    """


@dataclass(frozen=True)
class VolumeInfo:
    name: str
    labels: dict[str, str]


class SubnetTaken(Exception):
    """Docker refused a subnet because it overlaps a network already there."""


@dataclass(frozen=True)
class NetworkInfo:
    name: str
    labels: dict[str, str]


@dataclass(frozen=True)
class ContainerInfo:
    id: str
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

    # Every method below is handed a container id, and the daemon's API takes
    # that id directly. Looking the container up first (`containers.get`) was
    # an inspect per call — five a checkpointing round — queued on the same
    # daemon and controller threads as the execs that do the work. Only
    # `address_of` still inspects, because the inspect is what it reads.
    async def execute(self, container_id: str, command: SandboxCommand) -> CommandResult:
        handle = await self._exec_create(
            container_id,
            cmd=_bounded(command),
            workdir=command.cwd,
            stdout=True,
            stderr=True,
            stdin=command.stdin is not None,
            # No tty: a tty merges streams through a pty and rewrites newlines,
            # which turns exact output into approximately-right output.
            tty=False,
        )
        # Kept up to the limit as it arrives and the rest only counted, so a
        # command that never stops printing costs the controller the limit,
        # not everything it printed before its deadline.
        kept = bytearray()
        observed = 0

        def collect(chunks: Iterable[bytes]) -> None:
            nonlocal observed
            for chunk in chunks:
                observed += len(chunk)
                room = command.output_limit - len(kept)
                if room > 0:
                    kept.extend(chunk[:room])

        stdin = command.stdin
        if stdin is None:

            def work() -> None:
                collect(self.client.api.exec_start(handle["Id"], stream=True, demux=False))
        else:

            def work() -> None:
                collect(self._exec_with_stdin(handle["Id"], stdin))

        started = monotonic()
        try:
            await asyncio.wait_for(
                self._call(work), timeout=command.timeout_seconds + _BACKSTOP_SECONDS
            )
        except TimeoutError:
            exit_code, timed_out = 124, True
        else:
            info = await self._call(self.client.api.exec_inspect, handle["Id"])
            exit_code = int(info.get("ExitCode") or 0)
            timed_out = _ran_out(exit_code, monotonic() - started, command)

        body = bytes(kept)
        truncated = observed > command.output_limit
        if truncated:
            body += b"\n[output truncated by the platform]"
        return CommandResult(
            exit_code=exit_code,
            output=body.decode("utf-8", errors="replace"),
            truncated=truncated,
            timed_out=timed_out,
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
        volume = await self._call(
            self.client.volumes.create, name=name, labels={**labels, **(self.extra_labels or {})}
        )
        return str(volume.name)

    async def remove_volume(self, name: str) -> None:
        volume = await self._call(self.client.volumes.get, name)
        await self._call(volume.remove, force=True)

    async def remove_volume_if_present(self, name: str) -> bool:
        """`remove_volume`, where a volume that is already gone is fine.

        True when this call removed it. A volume still mounted by a container
        is refused by the daemon and raises, like any other refusal.
        """
        from docker.errors import NotFound  # noqa: PLC0415 - narrow the import

        try:
            await self.remove_volume(name)
        except DockerUnavailable as failure:
            if isinstance(failure.__cause__, NotFound):
                return False
            raise
        return True

    async def containers_labelled(
        self, label: str, value: str | None = None
    ) -> list[ContainerInfo]:
        """Every container carrying the label, running or not."""
        selector = label if value is None else f"{label}={value}"
        found: list[dict[str, Any]] = await self._call(
            self.client.api.containers, all=True, filters={"label": self._selectors(selector)}
        )
        return [
            ContainerInfo(id=str(entry["Id"]), labels=dict(entry.get("Labels") or {}))
            for entry in found
        ]

    # -- sandbox networks (product design §1.21.4) ----------------------------

    async def create_network(self, name: str, subnet: str, labels: dict[str, str]) -> None:
        """An `internal` bridge on `subnet`: no gateway out, one sandbox on it.

        `SubnetTaken` when the subnet overlaps a network already on the host,
        for the caller to try another; anything else is `DockerUnavailable`.
        """
        from docker.errors import APIError  # noqa: PLC0415 - narrow the import
        from docker.types import IPAMConfig, IPAMPool  # noqa: PLC0415

        ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)])
        all_labels = {**labels, **(self.extra_labels or {})}
        try:
            await asyncio.to_thread(
                self.client.networks.create,
                name,
                driver="bridge",
                internal=True,
                ipam=ipam,
                labels=all_labels,
            )
        except APIError as failure:
            if "overlap" in str(failure).lower():
                raise SubnetTaken(subnet) from failure
            raise DockerUnavailable(str(failure)) from failure

    async def connect(
        self, network: str, container: str, aliases: list[str], *, address: str | None = None
    ) -> None:
        await self._call(
            self.client.api.connect_container_to_network,
            container,
            network,
            aliases=aliases,
            ipv4_address=address,
        )

    async def container_starts(self, name: str) -> AsyncIterator[None]:
        """One item each time the named container starts, until the stream ends.

        Docker's event stream is a blocking generator, read here one event per
        worker thread. It ends when the daemon restarts; the caller reopens it.
        """
        events: Any = await self._call(
            self.client.events,
            decode=True,
            filters={"type": "container", "event": "start", "container": name},
        )
        try:
            while True:
                event = await asyncio.to_thread(next, events, None)
                if event is None:
                    return
                yield
        finally:
            events.close()

    async def network_endpoints(self, name: str) -> tuple[str | None, frozenset[str]]:
        """A network's subnet and the names of the containers on it."""
        details: dict[str, Any] = await self._call(self.client.api.inspect_network, name)
        ipam: dict[str, Any] = details.get("IPAM") or {}
        pools: list[dict[str, Any]] = ipam.get("Config") or []
        first: dict[str, Any] = pools[0] if pools else {}
        subnet = str(first["Subnet"]) if first.get("Subnet") else None
        endpoints: dict[str, dict[str, Any]] = details.get("Containers") or {}
        return subnet, frozenset(str(endpoint.get("Name", "")) for endpoint in endpoints.values())

    async def remove_network_if_present(self, name: str) -> bool:
        """Every endpoint off it first, the proxy's included: Docker will not
        remove a network something is still attached to."""
        from docker.errors import NotFound  # noqa: PLC0415 - narrow the import

        try:
            details: dict[str, Any] = await self._call(self.client.api.inspect_network, name)
        except DockerUnavailable as failure:
            if isinstance(failure.__cause__, NotFound):
                return False
            raise
        endpoints: dict[str, Any] = details.get("Containers") or {}
        for endpoint in endpoints:
            try:
                await self._call(
                    self.client.api.disconnect_container_from_network, endpoint, name, force=True
                )
            except DockerUnavailable as failure:
                if not isinstance(failure.__cause__, NotFound):
                    raise
        try:
            await self._call(self.client.api.remove_network, name)
        except DockerUnavailable as failure:
            if isinstance(failure.__cause__, NotFound):
                return False
            raise
        return True

    async def networks_labelled(
        self, label: str, value: str | None = None
    ) -> list[NetworkInfo]:
        selector = label if value is None else f"{label}={value}"
        found: list[dict[str, Any]] = await self._call(
            self.client.api.networks, filters={"label": self._selectors(selector)}
        )
        return [
            NetworkInfo(name=str(entry["Name"]), labels=dict(entry.get("Labels") or {}))
            for entry in found
        ]

    async def volumes_labelled(self, label: str) -> list[VolumeInfo]:
        """Every volume carrying the label — by label, never by parsed name."""
        found: list[Any] = await self._call(
            self.client.volumes.list, filters={"label": self._selectors(label)}
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
        loop = asyncio.get_running_loop()

        def upload() -> None:
            self.client.api.put_archive(container_id, target, _pulled_from(loop, tar_stream))

        await self._call(upload)

    async def export_tree(
        self, container_id: str, source: str
    ) -> AsyncIterator[bytes]:

        def open_archive() -> Iterator[bytes]:
            stream, _ = self.client.api.get_archive(container_id, source, chunk_size=_SCAN_CHUNK)
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

        def scan() -> tuple[ScannedEntry, ...]:
            stream, _ = self.client.api.get_archive(container_id, source, chunk_size=_SCAN_CHUNK)
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
        handle = await self._exec_create(
            container_id,
            cmd=_bounded(command),
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

        started = monotonic()
        try:
            await asyncio.wait_for(
                self._call(drain), timeout=command.timeout_seconds + _BACKSTOP_SECONDS
            )
        except TimeoutError:
            exit_code, timed_out = 124, True
        else:
            info = await self._call(self.client.api.exec_inspect, handle["Id"])
            exit_code = int(info.get("ExitCode") or 0)
            timed_out = _ran_out(exit_code, monotonic() - started, command)
        return StreamedResult(
            exit_code=exit_code,
            timed_out=timed_out,
            observed_bytes=observed,
            delivered_bytes=delivered,
            truncated=observed > delivered,
        )

    async def pause(self, container_id: str) -> None:
        try:
            await self._call(self.client.api.pause, container_id)
        except DockerUnavailable as failure:
            # A sandbox past its memory with nothing left to kill dies whole,
            # and the freeze at the end of its slice is the first call to
            # meet that.
            raise await self._why_unusable(container_id, failure) from failure

    async def _exec_create(self, container_id: str, **options: Any) -> Any:
        """`exec_create`, naming a container that cannot run anything."""
        try:
            return await self._call(self.client.api.exec_create, container_id, **options)
        except DockerUnavailable as failure:
            raise await self._why_unusable(container_id, failure) from failure

    async def _why_unusable(
        self, container_id: str, failure: DockerUnavailable
    ) -> DockerUnavailable:
        """Which failure a refused call on a container was.

        Asked only after the call failed, so the path that works pays no extra
        inspection. If the daemon cannot answer this either, the original
        failure stands.
        """
        from docker.errors import NotFound  # noqa: PLC0415 - narrow the import

        if isinstance(failure.__cause__, NotFound):
            return ContainerGone(f"no such container: {container_id[:12]}")
        try:
            state: dict[str, Any] = await self._call(
                self.client.api.inspect_container, container_id
            )
        except DockerUnavailable as asked:
            if isinstance(asked.__cause__, NotFound):
                return ContainerGone(f"no such container: {container_id[:12]}")
            return failure
        status: dict[str, Any] = state.get("State") or {}
        if not status.get("Running"):
            return ContainerNotRunning(
                f"container exited with {status.get('ExitCode')}"
                f"{' after an OOM kill' if status.get('OOMKilled') else ''}"
            )
        return failure

    async def unpause(self, container_id: str) -> None:
        await self._call(self.client.api.unpause, container_id)

    async def unpause_if_present(self, container_id: str) -> bool:
        """`unpause`, answering False for a container that no longer exists."""
        from docker.errors import NotFound  # noqa: PLC0415 - narrow the import

        try:
            await self.unpause(container_id)
        except DockerUnavailable as failure:
            if isinstance(failure.__cause__, NotFound):
                return False
            raise
        return True

    async def remove(self, container_id: str) -> None:
        """Force-remove, thawing a frozen container first.

        gVisor cannot kill a paused container: `docker rm -f` answers "is
        zombie and can not be killed" (2026-10-24 trial), so every reclamation
        or eviction of a frozen sandbox failed there. runc does not need this,
        and the extra inspection costs it one call.
        """
        state: dict[str, Any] = await self._call(self.client.api.inspect_container, container_id)
        status: dict[str, Any] = state.get("State") or {}
        if status.get("Paused"):
            await self._call(self.client.api.unpause, container_id)
        await self._call(self.client.api.remove_container, container_id, force=True)

    async def remove_if_present(self, container_id: str) -> None:
        """`remove`, where a container that is already gone is what was wanted.

        For eviction, which can reach a container in the same moment as the
        Scheduler's reclamation of it.
        """
        from docker.errors import NotFound  # noqa: PLC0415 - narrow the import

        try:
            await self.remove(container_id)
        except DockerUnavailable as failure:
            if isinstance(failure.__cause__, NotFound):
                return
            raise

    def _exec_with_stdin(self, exec_id: str, stdin: bytes) -> Iterator[bytes]:
        """Feed stdin whole, close it, then yield the demuxed output.

        Runs in the engine's worker thread. Written for the file helper, which
        reads all of stdin before it produces output — a command that streams
        output while its input is still arriving could fill the daemon's
        buffer, and no M1 tool does that.
        """
        import docker.utils.socket as docker_socket  # noqa: PLC0415

        untyped: Any = docker_socket
        sock = self.client.api.exec_start(exec_id, socket=True, demux=False)
        with sock:
            inner: Any = getattr(sock, "_sock", sock)
            inner.sendall(stdin)
            import socket as stdlib_socket  # noqa: PLC0415

            inner.shutdown(stdlib_socket.SHUT_WR)
            for frame in cast(
                list[tuple[int, bytes]], untyped.frames_iter(sock, tty=False)
            ):
                yield frame[1]

    def _selectors(self, selector: str) -> list[str]:
        """The label filter, narrowed to this engine's own labels when it has any.

        Docker ANDs a list of label filters. An engine given labels of its own
        lists only what carries them (2026-10-24): the integration suite's
        controllers share a developer's Docker with a running stack, and their
        sweep removed that stack's live sandbox. A deployment gives none, and
        lists, sweeps and reclaims its whole host as before.
        """
        own = [f"{key}={value}" for key, value in (self.extra_labels or {}).items()]
        return [selector, *own]

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
