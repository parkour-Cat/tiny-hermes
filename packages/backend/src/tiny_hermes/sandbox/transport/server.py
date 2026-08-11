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
import json
import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from typing import Any, cast

Dispatch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

#: The Controller's whole vocabulary. An action outside it is refused here
#: rather than passed on, so a typo cannot become an attribute lookup.
ACTIONS = frozenset(
    {"acquire", "execute", "freeze", "thaw", "destroy", "inspect", "keep", "cleanup"}
)


class ProtocolError(StrEnum):
    MALFORMED = "malformed_frame"
    UNKNOWN_ACTION = "unknown_action"
    TOO_LARGE = "frame_too_large"


class ControllerServer:
    #: A local socket is still a socket, and an unbounded read is still an
    #: unbounded read. A tool request carries a command line, not a payload.
    MAX_FRAME_BYTES = 64 * 1024

    def __init__(self, *, dispatch: Dispatch, path: str, group: int | None = None) -> None:
        self._dispatch = dispatch
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
                await self._say(writer, await self._answer(line))
        except (ConnectionResetError, BrokenPipeError):
            # One client going away is not the Controller's problem.
            return
        finally:
            writer.close()

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
