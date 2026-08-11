"""A server the outbound tests can steer, on a real socket.

Real HTTP rather than a mock transport, for the same reason phase 2B ran the SSE
tests against uvicorn: a transport that short-circuits proves nothing about a
client whose whole job is what it does with a connection.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn


@dataclass
class Recorded:
    method: str
    path: str
    query: str
    headers: dict[str, str]


@dataclass
class StandIn:
    """One steerable endpoint, and a record of everything that reached it."""

    requests: list[Recorded] = field(default_factory=list[Recorded])
    #: How many more times `/flaky` refuses before it succeeds.
    failures_remaining: int = 0
    failure_status: int = 429
    #: Body size `/huge` answers with.
    huge_bytes: int = 64 * 1024
    slow_seconds: float = 5.0

    @property
    def paths(self) -> list[str]:
        return [entry.path for entry in self.requests]

    def last(self) -> Recorded:
        assert self.requests, "the stand-in received no request at all"
        return self.requests[-1]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope["headers"]
        }
        path = str(scope["path"])
        query = scope["query_string"].decode("latin-1")
        self.requests.append(Recorded(str(scope["method"]), path, query, headers))
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break

        status, body = await self._answer(path, query)
        extra: list[tuple[bytes, bytes]] = []
        if status in (301, 302, 303, 307, 308):
            extra.append((b"location", body.decode("latin-1").encode("latin-1")))
            body = b""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *extra,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _answer(self, path: str, query: str) -> tuple[int, bytes]:
        if path == "/redirect":
            # The target rides in the query string so a test can point a hop
            # anywhere, including somewhere the policy must refuse.
            return 302, query.removeprefix("to=").encode("latin-1")
        if path == "/loop":
            return 302, b"/loop"
        if path == "/permanent":
            return 307, query.removeprefix("to=").encode("latin-1")
        if path == "/huge":
            return 200, json.dumps({"filler": "x" * self.huge_bytes}).encode()
        if path == "/slow":
            await asyncio.sleep(self.slow_seconds)
            return 200, b'{"ok":true}'
        if path == "/unauthorized":
            return 401, b'{"error":"no"}'
        if path == "/flaky":
            if self.failures_remaining > 0:
                self.failures_remaining -= 1
                return self.failure_status, b'{"error":"later"}'
            return 200, b'{"ok":true}'
        return 200, b'{"ok":true}'


@contextlib.asynccontextmanager
async def _serving(app: StandIn) -> AsyncGenerator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield f"http://127.0.0.1:{int(address[1])}"
    finally:
        server.should_exit = True
        await task


StandInFactory = Callable[[], AsyncGenerator[tuple[StandIn, str]]]


@pytest.fixture
async def stand_in() -> AsyncIterator[tuple[StandIn, str]]:
    app = StandIn()
    async with _serving(app) as url:
        yield app, url


@pytest.fixture
async def second_stand_in() -> AsyncIterator[tuple[StandIn, str]]:
    """A second origin, so a cross-origin redirect has somewhere to land."""
    app = StandIn()
    async with _serving(app) as url:
        yield app, url
