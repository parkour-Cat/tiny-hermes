"""What the Worker and the Scheduler hold instead of a Controller.

A dumb encoder. It knows how to turn UUIDs into strings, how to frame a line,
and how to tell a refusal from a broken pipe. It knows nothing about
reservations, leases, or containers — a rule that appeared here would be a rule
the Controller could not enforce, since the Controller is the only thing on the
other side that a Scheduler cannot bypass by calling the socket directly.
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from typing import Any, cast
from uuid import UUID

from tiny_hermes.sandbox.transport.frames import (
    MAX_FRAME_PAYLOAD,
    Frame,
    FrameType,
    StreamReceiver,
    decode_frame,
    encode_frame,
)


def _plain(value: Any) -> Any:
    """JSON has no UUID and no enum, so the encoding is somebody's job.

    It is this one's, which is why the Controller's own signatures keep taking
    UUIDs and the wire never carries a bare string the Controller has to guess
    the meaning of.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(entry) for key, entry in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [_plain(entry) for entry in value]  # pyright: ignore[reportUnknownVariableType]
    return value


class ControllerClient:
    class Unreachable(Exception):
        """The Controller could not be spoken to.

        Distinct from `Refused`: this is the platform being unable to ask, and
        a Run that meets it has an unknown outcome rather than a decided one.
        """

    class Refused(Exception):
        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    def __init__(self, path: str, *, timeout: float = 120.0) -> None:
        self.path = path
        self._timeout = timeout

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        frame = json.dumps({"action": action, "payload": _plain(payload)}).encode()
        try:
            reader, writer = await asyncio.open_unix_connection(self.path)
        except OSError as unreachable:
            raise ControllerClient.Unreachable(str(unreachable)) from unreachable
        try:
            writer.write(frame + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
        except (OSError, TimeoutError) as failure:
            raise ControllerClient.Unreachable(str(failure)) from failure
        finally:
            writer.close()

        if not line:
            raise ControllerClient.Unreachable("the controller closed without answering")
        return self._result(line)

    async def send_stream(
        self,
        action: str,
        payload: dict[str, Any],
        chunks: AsyncIterator[bytes],
        *,
        declared_total: int,
    ) -> dict[str, Any]:
        """One streaming upload: control line, ready, frames, final result."""
        reader, writer = await self._connect()
        # Read the verdict concurrently with the upload. A server that refuses
        # mid-stream stops reading and answers at once; a client that only
        # looked for the answer after the last frame would meet the broken
        # pipe first and report a decided "no" as an unknown failure.
        final: asyncio.Task[bytes] | None = None
        try:
            await self._open(writer, action, payload)
            await self._expect_ready(reader)
            final = asyncio.create_task(reader.readline())

            try:
                sequence = 0
                sent = 0
                frames = 0
                hasher = hashlib.sha256()
                start = json.dumps({"total_limit": declared_total}).encode()
                writer.write(encode_frame(Frame(FrameType.START, sequence, start)))
                sequence += 1
                async for chunk in chunks:
                    if final.done():
                        break
                    for offset in range(0, len(chunk), MAX_FRAME_PAYLOAD):
                        piece = chunk[offset : offset + MAX_FRAME_PAYLOAD]
                        writer.write(encode_frame(Frame(FrameType.DATA, sequence, piece)))
                        await writer.drain()
                        sequence += 1
                        sent += len(piece)
                        frames += 1
                        hasher.update(piece)
                if not final.done():
                    end = json.dumps(
                        {
                            "total_bytes": sent,
                            "frame_count": frames,
                            "sha256": hasher.hexdigest(),
                        }
                    ).encode()
                    writer.write(encode_frame(Frame(FrameType.END, sequence, end)))
                    await writer.drain()
            except OSError as broken:
                verdict = await self._verdict_or_none(final)
                if verdict is not None:
                    return self._result(verdict)
                raise ControllerClient.Unreachable(str(broken)) from broken

            line = await asyncio.wait_for(final, timeout=self._timeout)
        except (OSError, TimeoutError) as failure:
            raise ControllerClient.Unreachable(str(failure)) from failure
        finally:
            if final is not None and not final.done():
                final.cancel()
            writer.close()
        if not line:
            raise ControllerClient.Unreachable("the controller closed mid-stream")
        return self._result(line)

    async def _verdict_or_none(self, final: asyncio.Task[bytes]) -> bytes | None:
        try:
            line = await asyncio.wait_for(asyncio.shield(final), timeout=5.0)
        except (OSError, TimeoutError):
            return None
        return line or None

    async def receive_stream(
        self,
        action: str,
        payload: dict[str, Any],
        sink: Callable[[bytes], Awaitable[None]],
        *,
        limit: int,
    ) -> dict[str, Any]:
        """One streaming download: frames into ``sink``, then the final result.

        ``limit`` is this caller's own ceiling; a server that declares a
        larger total is refused before the first data byte.
        """
        reader, writer = await self._connect()
        try:
            await self._open(writer, action, payload)
            await self._expect_ready(reader)

            receiver = StreamReceiver(declared_limit=limit)
            loop = asyncio.get_running_loop()
            while True:
                frame = await asyncio.wait_for(
                    self._read_frame(reader), timeout=self._timeout
                )
                decision = receiver.accept(frame, at=loop.time())
                if not decision.ok:
                    raise ControllerClient.Refused(f"stream_{decision.reason}")
                if frame.type is FrameType.DATA:
                    await sink(frame.payload)
                    receiver.credit(len(frame.payload))
                if decision.finished:
                    if frame.type is not FrameType.END:
                        raise ControllerClient.Refused("stream_cancelled")
                    break

            line = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as failure:
            raise ControllerClient.Unreachable(str(failure)) from failure
        finally:
            writer.close()
        if not line:
            raise ControllerClient.Unreachable("the controller closed mid-stream")
        return self._result(line)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.open_unix_connection(self.path)
        except OSError as unreachable:
            raise ControllerClient.Unreachable(str(unreachable)) from unreachable

    async def _open(
        self, writer: asyncio.StreamWriter, action: str, payload: dict[str, Any]
    ) -> None:
        frame = json.dumps({"action": action, "payload": _plain(payload)}).encode()
        writer.write(frame + b"\n")
        await writer.drain()

    async def _expect_ready(self, reader: asyncio.StreamReader) -> None:
        line = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
        if not line:
            raise ControllerClient.Unreachable("the controller closed without answering")
        opened = self._result(line)
        if opened.get("stream") != "ready":
            raise ControllerClient.Refused("stream_not_ready")

    async def _read_frame(self, reader: asyncio.StreamReader) -> Frame:
        header = await reader.readexactly(13)
        length = int.from_bytes(header[:4], "big")
        if length > MAX_FRAME_PAYLOAD:
            raise ControllerClient.Refused("stream_frame_too_large")
        body = await reader.readexactly(length) if length else b""
        decoded = decode_frame(header + body)
        if decoded is None:  # pragma: no cover - readexactly returned short
            raise ControllerClient.Unreachable("incomplete frame")
        return decoded[0]

    def _result(self, line: bytes) -> dict[str, Any]:
        answer: Any = json.loads(line)
        if not isinstance(answer, dict):
            raise ControllerClient.Unreachable("the controller answered with nonsense")
        body = cast(dict[str, Any], answer)
        if "error" in body:
            raise ControllerClient.Refused(str(body["error"]))
        result: Any = body.get("result")
        return cast(dict[str, Any], result) if isinstance(result, dict) else {}
