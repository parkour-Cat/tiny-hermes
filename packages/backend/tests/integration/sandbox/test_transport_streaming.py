"""The framed half of the controller socket, over a real Unix socket.

Skipped on Windows exactly like the line-mode transport tests. Nothing here
asserts a rule: the dispatch is a spy, and what these tests prove is that a
multi-frame import and export round-trip byte for byte, that a refusal beats
the first frame, and that a lying declared total dies at START.
"""

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.sandbox.transport.server import ControllerServer, ServerStream

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="socket.AF_UNIX does not exist in Python on Windows; CI covers this",
)

BODY = os.urandom(3 * 1024 * 1024 + 17)  # several frames, not frame-aligned


class SpyStreams:
    """Answers streaming actions with scripted byte behavior."""

    def __init__(self) -> None:
        self.received = bytearray()
        self.refusal: str | None = None
        self.to_send = b""
        self.receive_limit = 8 * 1024 * 1024

    async def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del action, payload
        return {}

    async def dispatch_stream(
        self, action: str, payload: dict[str, Any], channel: ServerStream
    ) -> dict[str, Any]:
        del payload
        if self.refusal is not None:
            raise _Refused(self.refusal)
        if action in ("workspace_import", "execute_stdin"):
            async for chunk in channel.receive(declared_limit=self.receive_limit):
                self.received.extend(chunk)
            if action == "execute_stdin":
                return {
                    "exit_code": 0,
                    "output": f"{channel.received_bytes} bytes taken",
                    "truncated": False,
                    "timed_out": False,
                }
            return {"received_bytes": channel.received_bytes}
        await channel.start_send(total_limit=len(self.to_send))
        await channel.push(self.to_send)
        return await channel.finish_send()


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@pytest.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[SpyStreams, ControllerClient]]:
    spy = SpyStreams()
    path = str(tmp_path / "controller.sock")
    server = ControllerServer(
        dispatch=spy.dispatch, stream_dispatch=spy.dispatch_stream, path=path
    )
    await server.start()
    try:
        yield spy, ControllerClient(path)
    finally:
        await server.stop()


async def _chunks(data: bytes, size: int = 700_000) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def test_a_multi_frame_import_round_trips_byte_for_byte(
    wired: tuple[SpyStreams, ControllerClient],
) -> None:
    spy, client = wired

    answer = await client.send_stream(
        "workspace_import",
        {"run_id": "r"},
        _chunks(BODY),
        declared_total=len(BODY),
    )

    assert bytes(spy.received) == BODY
    assert answer["received_bytes"] == len(BODY)


async def test_a_multi_frame_export_round_trips_byte_for_byte(
    wired: tuple[SpyStreams, ControllerClient],
) -> None:
    spy, client = wired
    spy.to_send = BODY
    received = bytearray()

    async def sink(chunk: bytes) -> None:
        received.extend(chunk)

    answer = await client.receive_stream(
        "workspace_export", {"run_id": "r"}, sink, limit=len(BODY)
    )

    assert bytes(received) == BODY
    assert answer["total_bytes"] == len(BODY)


async def test_a_refusal_beats_the_first_frame(
    wired: tuple[SpyStreams, ControllerClient],
) -> None:
    spy, client = wired
    spy.refusal = "lease_invalid"

    with pytest.raises(ControllerClient.Refused) as refused:
        await client.receive_stream(
            "workspace_export", {"run_id": "r"}, _swallow, limit=1024
        )
    assert refused.value.reason == "lease_invalid"


async def test_a_declared_total_above_the_server_limit_dies_at_start(
    wired: tuple[SpyStreams, ControllerClient],
) -> None:
    spy, client = wired
    spy.receive_limit = 1024

    with pytest.raises(ControllerClient.Refused) as refused:
        await client.send_stream(
            "workspace_import",
            {"run_id": "r"},
            _chunks(BODY),
            declared_total=len(BODY),
        )
    assert refused.value.reason == "stream_failed"
    assert not spy.received, "no byte may land after a refused START"


async def test_an_aborted_stream_does_not_take_the_server_down(
    wired: tuple[SpyStreams, ControllerClient],
) -> None:
    """A client that vanishes mid-frames burns its connection and nothing else."""
    spy, client = wired

    reader, writer = await asyncio.open_unix_connection(client.path)
    writer.write(b'{"action": "workspace_import", "payload": {"x": 1}}\n')
    await writer.drain()
    ready = await reader.readline()
    assert b"ready" in ready
    writer.write(b"\x00\x00\x00\x05")  # a length with no frame behind it
    writer.close()

    # The server is still there for the next caller.
    spy.received.clear()
    answer = await client.send_stream(
        "workspace_import", {"run_id": "r"}, _chunks(b"still alive"), declared_total=11
    )
    assert answer["received_bytes"] == 11
    assert bytes(spy.received) == b"still alive"


async def _swallow(chunk: bytes) -> None:
    del chunk


async def test_a_write_body_rides_frames_through_the_adapter(
    wired: tuple[SpyStreams, ControllerClient],
) -> None:
    """The Worker-facing adapter routes a stdin-bearing execute over frames."""
    from uuid import uuid4  # noqa: PLC0415

    from tiny_hermes.sandbox.domain.command import SandboxCommand  # noqa: PLC0415
    from tiny_hermes.sandbox.transport.adapter import SandboxClient  # noqa: PLC0415

    spy, client = wired
    body = b"w" * 300_000  # bigger than any control line may be
    adapter = SandboxClient(client)

    result = await adapter.execute(
        run_id=uuid4(),
        lease_id=uuid4(),
        sandbox_id=uuid4(),
        command=SandboxCommand(
            argv=["helper", "write"],
            cwd="/workspace/data",
            timeout_seconds=30,
            output_limit=4096,
            stdin=body,
        ),
    )

    assert bytes(spy.received) == body
    assert result.exit_code == 0
    assert "300000 bytes taken" in result.output
