"""The socket between the Worker and the Controller.

**Skipped on Windows**, where `socket.AF_UNIX` does not exist in Python — which
is the development machine for this repository. That is why the Controller's
rules live in a class this file does not touch: what goes unverified locally is
framing, not policy. CI runs these.

Nothing here asserts a rule. If a test in this file could be written to check
who may do what, the check is in the wrong place.
"""

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.sandbox.transport.server import ControllerServer, ProtocolError

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="socket.AF_UNIX does not exist in Python on Windows; CI covers this",
)


class SpyController:
    """Stands in for the Controller so the test is about the wire.

    It records what arrived and answers what it was told to. A real Controller
    here would make a framing failure look like a sandbox failure.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer: dict[str, Any] = {"sandbox_id": str(uuid4()), "cache_state": "reset"}
        self.refusal: str | None = None
        self.delay: float = 0.0

    async def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, payload))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.refusal is not None:
            raise _Refused(self.refusal)
        return self.answer


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@pytest.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[SpyController, ControllerClient]]:
    spy = SpyController()
    path = str(tmp_path / "controller.sock")
    server = ControllerServer(dispatch=spy.dispatch, path=path)
    await server.start()
    try:
        yield spy, ControllerClient(path)
    finally:
        await server.stop()


async def test_a_request_reaches_the_controller_and_the_answer_comes_back(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    spy, client = wired
    run_id = uuid4()

    answer = await client.call("acquire", {"run_id": str(run_id), "profile": "default"})

    assert spy.calls == [("acquire", {"run_id": str(run_id), "profile": "default"})]
    assert answer == spy.answer


async def test_a_refusal_arrives_as_a_refusal_and_not_as_a_dropped_connection(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    """A closed socket and a "no" are different facts.

    A Worker that cannot tell them apart would retry a refusal, which turns one
    clear rejection into a loop.
    """
    spy, client = wired
    spy.refusal = "lease_invalid"

    with pytest.raises(ControllerClient.Refused) as refused:
        await client.call("execute", {"sandbox_id": str(uuid4())})

    assert refused.value.reason == "lease_invalid"


async def test_the_server_survives_a_malformed_frame(
    wired: tuple[SpyController, ControllerClient], tmp_path: Path
) -> None:
    """One bad client must not take the Controller down for the other one."""
    spy, client = wired
    reader, writer = await asyncio.open_unix_connection(client.path)
    writer.write(b"this is not json\n")
    await writer.drain()
    error = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()

    assert error["error"] == ProtocolError.MALFORMED.value
    # And the Controller is still there.
    assert await client.call("inspect", {}) == spy.answer


async def test_a_frame_larger_than_the_cap_is_refused(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    """An unbounded read on a local socket is still an unbounded read.

    The refusal is an answer, not a dropped connection. CI caught the first
    version of this test asserting a broken pipe: the server does the better
    thing and says `frame_too_large`, which a caller can log and act on rather
    than having to guess why its socket died.
    """
    spy, client = wired
    reader, writer = await asyncio.open_unix_connection(client.path)
    writer.write(b"x" * (ControllerServer.MAX_FRAME_BYTES + 1))
    await writer.drain()
    answer = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
    writer.close()

    assert answer["error"] == ProtocolError.TOO_LARGE.value
    assert spy.calls == []


async def test_two_clients_are_served_at_once(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    """The Worker and the Scheduler are both callers, and neither waits for the
    other to finish a slow Docker call."""
    spy, client = wired
    spy.delay = 0.3

    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        client.call("inspect", {"who": "worker"}),
        client.call("inspect", {"who": "scheduler"}),
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert len(spy.calls) == 2
    assert elapsed < 0.6, "the two calls were serialized"


async def test_an_unknown_action_is_refused_rather_than_dispatched(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    spy, client = wired

    with pytest.raises(ControllerClient.Refused) as refused:
        await client.call("rm_rf", {})

    assert refused.value.reason == ProtocolError.UNKNOWN_ACTION.value
    assert spy.calls == []


async def test_uuids_and_enums_survive_the_round_trip(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    """JSON has no UUID, so the encoding has to be somebody's job.

    It is the client's, which is why the Controller's own signatures keep taking
    UUIDs and the wire never sees a bare string the Controller has to guess at.
    """
    spy, client = wired
    run_id = uuid4()
    spy.answer = {"sandbox_id": str(run_id), "cache_state": "reused"}

    answer = await client.call("acquire", {"run_id": run_id})

    assert spy.calls[0][1]["run_id"] == str(run_id)
    assert UUID(answer["sandbox_id"]) == run_id


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
async def test_the_socket_is_not_world_writable(
    wired: tuple[SpyController, ControllerClient],
) -> None:
    """The volume is shared with the Worker and the Scheduler and with nothing
    else, but the socket should not be the weak half of that arrangement."""
    _, client = wired
    mode = os.stat(client.path).st_mode  # noqa: PTH116 - one stat, no event loop
    assert not mode & 0o007
