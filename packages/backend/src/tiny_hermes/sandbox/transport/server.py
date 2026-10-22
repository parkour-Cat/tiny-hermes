"""A socket in front of the Controller: the Unix one, or TLS (tier B §4.1).

Reaching this socket is not authorization — technical design §11.1 is explicit,
and this file is where that would be easiest to forget. Over the Unix socket,
anything that arrives has proven only that it is one of two processes the
platform runs, both of which mount the volume it lives in. Over TLS, its
certificate proves the same, plus which of the two (`tls.py`): the one rule
this file applies is that the Scheduler may only ask for `cleanup`. Whether a
call may touch *this* Run's sandbox is decided by `application/controller.py`,
which this file calls and does not second-guess.

Newline-delimited JSON over `asyncio.start_unix_server`, chosen over HTTP so the
phase-3A ban on building HTTP clients does not need a second exemption for a
socket that cannot reach a network anyway.
"""

import asyncio
import hashlib
import json
import os
import socket
import ssl
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from typing import Any, cast

from tiny_hermes.sandbox.transport.actions import ACTIONS, STREAM_ACTIONS
from tiny_hermes.sandbox.transport.address import ControllerAddress
from tiny_hermes.sandbox.transport.frames import (
    IDLE_SECONDS,
    MAX_FRAME_PAYLOAD,
    PROGRESS_SECONDS,
    Frame,
    FrameType,
    StreamReceiver,
    decode_frame,
    encode_frame,
)
from tiny_hermes.sandbox.transport.tls import allowed_actions, role_of
from tiny_hermes.shared.metrics import CONTROLLER_ACTION_SECONDS

Dispatch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
StreamDispatch = Callable[
    [str, dict[str, Any], "ServerStream"], Awaitable[dict[str, Any]]
]

class ProtocolError(StrEnum):
    MALFORMED = "malformed_frame"
    UNKNOWN_ACTION = "unknown_action"
    TOO_LARGE = "frame_too_large"
    STREAM_FAILED = "stream_failed"
    ROLE_NOT_PERMITTED = "role_not_permitted"


def keep_alive(writer: asyncio.StreamWriter) -> None:
    """Probe an idle TCP connection, so a peer host that vanished is noticed.

    Design §4.4: the stream frames and the call timeout already bound a live
    exchange; this is for the connection nobody is reading from.
    """
    # Untyped on purpose: `socket.socket` is a banned name in this package
    # (outbound goes through tiny_hermes.outbound), and this one only sets
    # options on a connection asyncio already made.
    raw: Any = writer.get_extra_info("socket")
    if raw is None or raw.family not in (socket.AF_INET, socket.AF_INET6):
        return
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # Linux spells the idle time TCP_KEEPIDLE; macOS spells it TCP_KEEPALIVE.
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    if idle is not None:
        raw.setsockopt(socket.IPPROTO_TCP, idle, 30)


class StreamAborted(Exception):
    """The framed half of a call broke its own rules; the connection is over."""


class ServerStream:
    """One streaming exchange on one connection, driven by the dispatcher.

    Exactly one of `receive` or `start_send`/`push`/`finish_send` is used per
    call. `progress` / `keep_alive` ride the send path so a silent exec still
    produces frames. The "stream ready" line is sent only from here — after
    the dispatch authorized the action — so a refused caller never gets to
    transmit a byte.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._idle = idle_seconds
        self._sequence = 0
        self._sent = 0
        self._hasher = hashlib.sha256()
        self._data_frames = 0
        self._write_lock = asyncio.Lock()
        self.received_bytes = 0

    async def receive(self, *, declared_limit: int) -> AsyncIterator[bytes]:
        """Verified DATA payloads until a clean END; anything else aborts."""
        await self._ready()
        receiver = StreamReceiver(declared_limit=declared_limit)
        loop = asyncio.get_running_loop()
        while True:
            try:
                frame = await asyncio.wait_for(self._read_frame(), timeout=self._idle)
            except TimeoutError as idle:
                raise StreamAborted("stream idle past its deadline") from idle
            except (asyncio.IncompleteReadError, ValueError) as broken:
                raise StreamAborted(f"unreadable frame: {broken}") from broken
            decision = receiver.accept(frame, at=loop.time())
            if not decision.ok:
                raise StreamAborted(f"stream refused: {decision.reason}")
            if frame.type is FrameType.DATA:
                yield frame.payload
                # Consumption is sequential by construction, so the window
                # reopens as soon as the payload was handed downstream.
                receiver.credit(len(frame.payload))
            if decision.finished:
                if frame.type is not FrameType.END:
                    raise StreamAborted("stream cancelled by the peer")
                self.received_bytes = receiver.received_bytes
                return

    async def start_send(self, *, total_limit: int) -> None:
        await self._ready()
        payload = json.dumps({"total_limit": total_limit}).encode()
        async with self._write_lock:
            await self._write_frame(Frame(FrameType.START, 0, payload))
            self._sequence = 1

    async def push(self, chunk: bytes) -> None:
        async with self._write_lock:
            for start in range(0, len(chunk), MAX_FRAME_PAYLOAD):
                piece = chunk[start : start + MAX_FRAME_PAYLOAD]
                await self._write_frame(Frame(FrameType.DATA, self._sequence, piece))
                self._sequence += 1
                self._sent += len(piece)
                self._data_frames += 1
                self._hasher.update(piece)

    async def progress(self) -> None:
        """A keep-alive with no payload and no digest contribution (design §5.3)."""
        async with self._write_lock:
            await self._write_frame(Frame(FrameType.PROGRESS, self._sequence, b""))
            self._sequence += 1

    async def keep_alive(
        self, stop: asyncio.Event, *, interval: float = PROGRESS_SECONDS
    ) -> None:
        """Send PROGRESS until `stop`, so a command that prints nothing is not idle."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await self.progress()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    return

    async def finish_send(self) -> dict[str, Any]:
        totals: dict[str, Any] = {
            "total_bytes": self._sent,
            "frame_count": self._data_frames,
            "sha256": self._hasher.hexdigest(),
        }
        async with self._write_lock:
            await self._write_frame(
                Frame(FrameType.END, self._sequence, json.dumps(totals).encode())
            )
            self._sequence += 1
        return totals

    async def _ready(self) -> None:
        self._writer.write(json.dumps({"result": {"stream": "ready"}}).encode() + b"\n")
        await self._writer.drain()

    async def _read_frame(self) -> Frame:
        header = await self._reader.readexactly(13)
        length = int.from_bytes(header[:4], "big")
        if length > MAX_FRAME_PAYLOAD:
            raise StreamAborted(f"frame of {length} bytes")
        body = await self._reader.readexactly(length) if length else b""
        decoded = decode_frame(header + body)
        if decoded is None:  # pragma: no cover - readexactly returned short
            raise StreamAborted("incomplete frame")
        return decoded[0]

    async def _write_frame(self, frame: Frame) -> None:
        self._writer.write(encode_frame(frame))
        await self._writer.drain()


class ControllerServer:
    #: A local socket is still a socket, and an unbounded read is still an
    #: unbounded read. A tool request carries a command line, not a payload.
    MAX_FRAME_BYTES = 64 * 1024
    #: How long a stream's connection stays open for the client to stop
    #: sending after its answer; a client that never stops is cut off here.
    LINGER_SECONDS = 5.0

    def __init__(
        self,
        *,
        dispatch: Dispatch,
        path: str | None = None,
        address: ControllerAddress | None = None,
        tls: ssl.SSLContext | None = None,
        group: int | None = None,
        stream_dispatch: StreamDispatch | None = None,
    ) -> None:
        if address is None:
            if path is None:
                raise ValueError("a controller server needs a path or an address")
            address = ControllerAddress(scheme="unix", path=path)
        if address.is_tls != (tls is not None):
            # Never a TLS address served in the clear, and never a context
            # quietly ignored on a socket that has no use for it.
            raise ValueError(f"{address} needs {'a' if address.is_tls else 'no'} TLS context")
        self._dispatch = dispatch
        self._stream_dispatch = stream_dispatch
        self._address = address
        self._path = address.path
        self._tls = tls
        # The gid the platform's own processes run as. The Controller runs as
        # root — see the Compose file for why — so the socket it creates is
        # root:root, and a Worker at 10001:10001 cannot open a 0660 socket
        # whatever the group bits say. Handing it the app group is narrower
        # than making the socket world-writable, which is the other way to make
        # this work and the wrong one.
        self._group = group
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        """The TCP port actually listened on — the one chosen, when asked for 0."""
        if self._server is None or not self._address.is_tls:
            raise RuntimeError("not listening on TCP")
        listening: Any = self._server
        return int(listening.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._tls is not None:
            self._server = await asyncio.start_server(
                self._serve, self._address.host, self._address.port, ssl=self._tls
            )
            return
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # A leftover socket file from a killed Controller would make bind fail,
        # and a Controller that will not start because it crashed last time is
        # a worse outage than the crash.
        with suppress(FileNotFoundError):
            os.unlink(self._path)
        self._server = await asyncio.start_unix_server(self._serve, path=self._path)
        if self._group is not None:
            # Only root can do this, which the deployed Controller is. If it
            # fails, the Worker cannot open the socket, so startup must fail
            # rather than advertising a Controller that no caller can use.
            os.chown(self._path, -1, self._group)
        # The volume is shared with the Worker and the Scheduler and nothing
        # else, but the socket should not be the weak half of that arrangement.
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._tls is None:
            with suppress(FileNotFoundError):
                os.unlink(self._path)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # None is every action: the Unix socket carries no role to restrict by.
        allowed = (
            None
            if self._tls is None
            else allowed_actions(role_of(writer.get_extra_info("peercert")))
        )
        keep_alive(writer)
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    return
                except asyncio.LimitOverrunError:
                    await self._say(writer, {"error": ProtocolError.TOO_LARGE.value})
                    return
                if len(line) > self.MAX_FRAME_BYTES:
                    await self._say(writer, {"error": ProtocolError.TOO_LARGE.value})
                    return
                streaming = self._parse_streaming(line)
                if streaming is not None:
                    action, payload = streaming
                    if allowed is not None and action not in allowed:
                        # Before "ready", so a refused caller never sends a byte.
                        await self._say(writer, {"error": ProtocolError.ROLE_NOT_PERMITTED.value})
                        return
                    await self._say(
                        writer, await self._answer_stream(action, payload, reader, writer)
                    )
                    # After a framed exchange — finished or aborted midway —
                    # the byte stream cannot be trusted to frame another call.
                    await self._discard_unread(reader)
                    return
                await self._say(writer, await self._answer(line, allowed))
        except (ConnectionResetError, BrokenPipeError, ssl.SSLError):
            # One client going away is not the Controller's problem.
            return
        finally:
            writer.close()

    async def _discard_unread(self, reader: asyncio.StreamReader) -> None:
        """Let a refused upload notice the answer before the connection closes.

        A refusal mid-upload leaves the client's frames unread. Over TCP,
        closing with unread input sends a reset, and the reset can destroy the
        answer in the client's buffer before it is read, so a decided "no"
        reached the Worker as an unknown failure. The client stops writing
        once it sees the answer and closes; until then, or for at most
        `LINGER_SECONDS`, its bytes are read and thrown away.
        """
        try:
            async with asyncio.timeout(self.LINGER_SECONDS):
                while await reader.read(64 * 1024):
                    pass
        except (TimeoutError, ConnectionResetError, BrokenPipeError, ssl.SSLError):
            return

    def _parse_streaming(self, line: bytes) -> tuple[str, dict[str, Any]] | None:
        try:
            frame: Any = json.loads(line)
        except ValueError:
            return None
        if not isinstance(frame, dict):
            return None
        fields = cast(dict[str, Any], frame)
        action: Any = fields.get("action")
        payload: Any = fields.get("payload")
        if (
            isinstance(action, str)
            and action in STREAM_ACTIONS
            and isinstance(payload, dict)
        ):
            return action, cast(dict[str, Any], payload)
        return None

    async def _answer_stream(
        self,
        action: str,
        payload: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> dict[str, Any]:
        if self._stream_dispatch is None:
            return {"error": ProtocolError.UNKNOWN_ACTION.value}
        channel = ServerStream(reader, writer)
        try:
            return {"result": await self._stream_dispatch(action, payload, channel)}
        except StreamAborted:
            return {"error": ProtocolError.STREAM_FAILED.value}
        except Exception as refused:  # noqa: BLE001 - every refusal is a reply
            reason = getattr(refused, "reason", None)
            return {"error": str(getattr(reason, "value", reason) or type(refused).__name__)}

    async def _answer(self, line: bytes, allowed: frozenset[str] | None) -> dict[str, Any]:
        try:
            frame: Any = json.loads(line)
        except ValueError:
            return {"error": ProtocolError.MALFORMED.value}
        if not isinstance(frame, dict):
            return {"error": ProtocolError.MALFORMED.value}
        fields = cast(dict[str, Any], frame)
        action: Any = fields.get("action")
        payload: Any = fields.get("payload")
        if not isinstance(action, str) or not isinstance(payload, dict):
            return {"error": ProtocolError.MALFORMED.value}
        if action not in ACTIONS:
            return {"error": ProtocolError.UNKNOWN_ACTION.value}
        if allowed is not None and action not in allowed:
            return {"error": ProtocolError.ROLE_NOT_PERMITTED.value}
        started = time.monotonic()
        try:
            answer = {"result": await self._dispatch(action, cast(dict[str, Any], payload))}
        except Exception as refused:  # noqa: BLE001 - every refusal is a reply
            CONTROLLER_ACTION_SECONDS.labels(action=action, outcome="refused").observe(
                time.monotonic() - started
            )
            # A refusal and a dropped connection are different facts, and a
            # Worker that cannot tell them apart would retry a rejection.
            reason = getattr(refused, "reason", None)
            return {"error": str(getattr(reason, "value", reason) or type(refused).__name__)}
        CONTROLLER_ACTION_SECONDS.labels(action=action, outcome="ok").observe(
            time.monotonic() - started
        )
        return answer

    async def _say(self, writer: asyncio.StreamWriter, body: dict[str, Any]) -> None:
        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()
