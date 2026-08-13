"""Startup failures at the Controller's local socket boundary."""

from typing import Any

import pytest
from tiny_hermes.sandbox.transport.server import ControllerServer


class _Server:
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def _dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    del action, payload
    return {}


async def test_chown_failure_prevents_controller_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket the Worker cannot open is not a successfully started Controller."""

    async def started(*_: object, **__: object) -> _Server:
        return _Server()

    def missing(_: object) -> None:
        raise FileNotFoundError

    def denied(*_: object) -> None:
        raise PermissionError("group change denied")

    def unchanged(*_: object) -> None:
        return None

    monkeypatch.setattr(
        "tiny_hermes.sandbox.transport.server.asyncio.start_unix_server",
        started,
        raising=False,
    )
    monkeypatch.setattr("tiny_hermes.sandbox.transport.server.os.unlink", missing)
    monkeypatch.setattr(
        "tiny_hermes.sandbox.transport.server.os.chown", denied, raising=False
    )
    monkeypatch.setattr("tiny_hermes.sandbox.transport.server.os.chmod", unchanged)

    server = ControllerServer(
        dispatch=_dispatch, path="controller.sock", group=10001
    )

    with pytest.raises(PermissionError, match="group change denied"):
        await server.start()
