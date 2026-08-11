"""What the Worker and the Scheduler hold instead of a Controller.

A dumb encoder. It knows how to turn UUIDs into strings, how to frame a line,
and how to tell a refusal from a broken pipe. It knows nothing about
reservations, leases, or containers — a rule that appeared here would be a rule
the Controller could not enforce, since the Controller is the only thing on the
other side that a Scheduler cannot bypass by calling the socket directly.
"""

import asyncio
import json
from enum import Enum
from typing import Any, cast
from uuid import UUID


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
        answer: Any = json.loads(line)
        if not isinstance(answer, dict):
            raise ControllerClient.Unreachable("the controller answered with nonsense")
        body = cast(dict[str, Any], answer)
        if "error" in body:
            raise ControllerClient.Refused(str(body["error"]))
        result: Any = body.get("result")
        return cast(dict[str, Any], result) if isinstance(result, dict) else {}
