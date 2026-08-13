"""A Unix socket in front of the Controller. No rule lives here.

Reaching this socket is not authorization — technical design §11.1 is explicit,
and this file is where that would be easiest to forget. Both the Worker and the
Scheduler mount the volume it lives in, so anything that arrives here has
already proven only that it is one of two processes the platform runs. Who may
do what is decided by `application/controller.py`, which this file calls and
does not second-guess.

Newline-delimited JSON over `asyncio.start_unix_server`, chosen over HTTP so the
phase-3A ban on building HTTP clients does not need a second exemption for a
socket that cannot reach a network anyway.
"""

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from typing import Any, cast

from tiny_hermes.sandbox.transport.frames import (
    IDLE_SECONDS,
    MAX_FRAME_PAYLOAD,
    Frame,
    FrameType,
    StreamReceiver,
    decode_frame,
    encode_frame,
)

Dispatch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
StreamDispatch = Callable[
    [str, dict[str, Any], "ServerStream"], Awaitable[dict[str, Any]]
]

#: Actions whose connection switches to Task 5 frames after the control line.
#: `execute_stdin` exists because a `file.write` body must not ride a JSON
#: control line whose whole point is to stay small.
STREAM_ACTIONS = frozenset(
    {"workspace_import", "workspace_export", "execute_stream", "execute_stdin"}
)

#: The Controller's whole vocabulary. An action outside it is refused here
#: rather than passed on, so a typo cannot become an attribute lookup.
ACTIONS = (
    frozenset(
        {
            "acquire",
            "execute",
            "freeze",
            "thaw",
            "destroy",
            "inspect",
            "keep",
            "cleanup",
            "workspace_scan",
            "volume_remove",
        }
    )
    | STREAM_ACTIONS
)


class ProtocolError(StrEnum):
    MALFORMED = "malformed_frame"
    UNKNOWN_ACTION = "unknown_action"
    TOO_LARGE = "frame_too_large"
    STREAM_FAILED = "stream_failed"


class StreamAborted(Exception):
    """The framed half of a call broke its own rules; the connection is over."""


class ServerStream:
    """One streaming exchange on one connection, driven by the dispatcher.

    Exactly one of `receive` or `start_send`/`push`/`finish_send` is used per
    call. The "stream ready" line is sent only from here — after the dispatch
    authorized the action — so a refused caller never gets to transmit a byte.
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
        await self._write_frame(Frame(FrameType.START, 0, payload))
        self._sequence = 1

    async def push(self, chunk: bytes) -> None:
        for start in range(0, len(chunk), MAX_FRAME_PAYLOAD):
            piece = chunk[start : start + MAX_FRAME_PAYLOAD]
            await self._write_frame(Frame(FrameType.DATA, self._sequence, piece))
            self._sequence += 1
            self._sent += len(piece)
            self._data_frames += 1
            self._hasher.update(piece)

    async def finish_send(self) -> dict[str, Any]:
        totals: dict[str, Any] = {
            "total_bytes": self._sent,
            "frame_count": self._data_frames,
            "sha256": self._hasher.hexdigest(),
        }
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

    def __init__(
        self,
        *,
        dispatch: Dispatch,
        path: str,
        group: int | None = None,
        stream_dispatch: StreamDispatch | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._stream_dispatch = stream_dispatch
        self._path = path
        # The gid the platform's own processes run as. The Controller runs as
        # root — see the Compose file for why — so the socket it creates is
        # root:root, and a Worker at 10001:10001 cannot open a 0660 socket
        # whatever the group bits say. Handing it the app group is narrower
        # than making the socket world-writable, which is the other way to make
        # this work and the wrong one.
        self._group = group
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
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
        with suppress(FileNotFoundError):
            os.unlink(self._path)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
                    await self._say(
                        writer, await self._answer_stream(action, payload, reader, writer)
                    )
                    # After a framed exchange — finished or aborted midway —
                    # the byte stream cannot be trusted to frame another call.
                    return
                await self._say(writer, await self._answer(line))
        except (ConnectionResetError, BrokenPipeError):
            # One client going away is not the Controller's problem.
            return
        finally:
            writer.close()

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

    async def _answer(self, line: bytes) -> dict[str, Any]:
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
        try:
            return {"result": await self._dispatch(action, cast(dict[str, Any], payload))}
        except Exception as refused:  # noqa: BLE001 - every refusal is a reply
            # A refusal and a dropped connection are different facts, and a
            # Worker that cannot tell them apart would retry a rejection.
            reason = getattr(refused, "reason", None)
            return {"error": str(getattr(reason, "value", reason) or type(refused).__name__)}

    async def _say(self, writer: asyncio.StreamWriter, body: dict[str, Any]) -> None:
        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()
