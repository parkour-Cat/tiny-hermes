"""What TLS adds to the Controller's wire: who may connect, and who may ask what.

Tier B design §4.2. The protocol itself runs over TLS in `test_transport.py`
and `test_transport_streaming.py`, which are parametrized over both transports;
this file is about the handshake and the roles, with real certificates and a
real `ssl` handshake on a loopback port.
"""

import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tiny_hermes.sandbox.transport.address import ControllerAddress
from tiny_hermes.sandbox.transport.certs import issue, issue_ca, load_pair, write_pair
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.sandbox.transport.server import ControllerServer, ProtocolError, ServerStream
from tiny_hermes.sandbox.transport.tls import Role, TlsFiles, client_context, server_context

from .conftest import Pki, wire


class Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        self.calls.append(action)
        return {"ok": True}

    async def dispatch_stream(
        self, action: str, payload: dict[str, Any], channel: ServerStream
    ) -> dict[str, Any]:
        del payload
        self.calls.append(action)
        async for _ in channel.receive(declared_limit=1024):
            pass
        return {"ok": True}


async def _serve(pki: Pki, spy: Spy, *, certificate: TlsFiles | None = None) -> ControllerServer:
    server = ControllerServer(
        address=ControllerAddress(scheme="tls", host="127.0.0.1", port=0),
        tls=server_context(certificate or pki.files(Role.CONTROLLER)),
        dispatch=spy.dispatch,
        stream_dispatch=spy.dispatch_stream,
    )
    await server.start()
    return server


def _client(server: ControllerServer, context: ssl.SSLContext) -> ControllerClient:
    return ControllerClient(
        ControllerAddress(scheme="tls", host="127.0.0.1", port=server.bound_port),
        tls=context,
        timeout=5.0,
    )


async def _chunks(data: bytes):  # noqa: ANN202 - an async generator for send_stream
    yield data


async def test_a_worker_certificate_reaches_the_controller(controller_pki: Pki) -> None:
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        client = _client(server, client_context(controller_pki.files(Role.WORKER)))
        assert await client.call("inspect", {}) == {"ok": True}
    finally:
        await server.stop()

    assert spy.calls == ["inspect"]


async def test_a_caller_without_a_certificate_is_not_heard(controller_pki: Pki) -> None:
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        anonymous = ssl.create_default_context(cafile=controller_pki.files(Role.WORKER).ca_file)
        with pytest.raises(ControllerClient.Unreachable):
            await _client(server, anonymous).call("inspect", {})
    finally:
        await server.stop()

    assert spy.calls == []


async def test_a_certificate_from_another_ca_is_not_heard(
    controller_pki: Pki, tmp_path: Path
) -> None:
    stranger = issue_ca()
    write_pair(tmp_path, "ca", stranger)
    write_pair(tmp_path, "worker", issue(stranger, role=Role.WORKER))
    # Trusts the real controller, presents a certificate it did not issue.
    files = TlsFiles(
        ca_file=controller_pki.files(Role.WORKER).ca_file,
        cert_file=str(tmp_path / "worker.pem"),
        key_file=str(tmp_path / "worker.key"),
    )
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        with pytest.raises(ControllerClient.Unreachable):
            await _client(server, client_context(files)).call("inspect", {})
    finally:
        await server.stop()

    assert spy.calls == []


async def test_an_expired_caller_certificate_is_not_heard(
    controller_pki: Pki, tmp_path: Path
) -> None:
    ca_key = controller_pki.directory / "ca.key"
    ca = load_pair(controller_pki.directory / "ca.pem", ca_key)
    long_ago = datetime.now(UTC) - timedelta(days=400)
    write_pair(tmp_path, "worker", issue(ca, role=Role.WORKER, days=30, now=long_ago))
    files = TlsFiles(
        ca_file=controller_pki.files(Role.WORKER).ca_file,
        cert_file=str(tmp_path / "worker.pem"),
        key_file=str(tmp_path / "worker.key"),
    )
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        with pytest.raises(ControllerClient.Unreachable):
            await _client(server, client_context(files)).call("inspect", {})
    finally:
        await server.stop()

    assert spy.calls == []


async def test_a_controller_under_another_name_is_not_trusted(
    controller_pki: Pki, tmp_path: Path
) -> None:
    """The caller checks whom it reached, not only that the CA signed it."""
    ca = load_pair(controller_pki.directory / "ca.pem", controller_pki.directory / "ca.key")
    write_pair(tmp_path, "controller", issue(ca, role=Role.CONTROLLER, hosts=["other.internal"]))
    elsewhere = TlsFiles(
        ca_file=controller_pki.files(Role.WORKER).ca_file,
        cert_file=str(tmp_path / "controller.pem"),
        key_file=str(tmp_path / "controller.key"),
    )
    spy = Spy()
    server = await _serve(controller_pki, spy, certificate=elsewhere)
    try:
        client = _client(server, client_context(controller_pki.files(Role.WORKER)))
        with pytest.raises(ControllerClient.Unreachable):
            await client.call("inspect", {})
    finally:
        await server.stop()

    assert spy.calls == []


async def test_a_worker_certificate_cannot_pose_as_the_controller(controller_pki: Pki) -> None:
    # Same CA, but issued for a client: a Worker's key must not be enough to
    # stand up a Controller that other Workers would talk to.
    spy = Spy()
    server = await _serve(controller_pki, spy, certificate=controller_pki.files(Role.WORKER))
    try:
        client = _client(server, client_context(controller_pki.files(Role.WORKER)))
        with pytest.raises(ControllerClient.Unreachable):
            await client.call("inspect", {})
    finally:
        await server.stop()


async def test_the_scheduler_may_clean_up(controller_pki: Pki) -> None:
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        client = _client(server, client_context(controller_pki.files(Role.SCHEDULER)))
        assert await client.call("cleanup", {}) == {"ok": True}
    finally:
        await server.stop()

    assert spy.calls == ["cleanup"]


@pytest.mark.parametrize("action", ["execute", "acquire", "destroy", "inspect"])
async def test_the_scheduler_may_not_call_a_workers_action(
    controller_pki: Pki, action: str
) -> None:
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        client = _client(server, client_context(controller_pki.files(Role.SCHEDULER)))
        with pytest.raises(ControllerClient.Refused) as refused:
            await client.call(action, {})
    finally:
        await server.stop()

    assert refused.value.reason == ProtocolError.ROLE_NOT_PERMITTED.value
    assert spy.calls == [], "a refused role must not reach the controller"


async def test_the_scheduler_may_not_open_a_stream(controller_pki: Pki) -> None:
    spy = Spy()
    server = await _serve(controller_pki, spy)
    try:
        client = _client(server, client_context(controller_pki.files(Role.SCHEDULER)))
        with pytest.raises(ControllerClient.Refused) as refused:
            await client.send_stream(
                "workspace_import", {"run_id": "r"}, _chunks(b"data"), declared_total=4
            )
    finally:
        await server.stop()

    assert refused.value.reason == ProtocolError.ROLE_NOT_PERMITTED.value
    assert spy.calls == []


async def test_a_tcp_connection_is_kept_alive(controller_pki: Pki, socket_dir: Path) -> None:
    """A half-open connection to another host must not hang forever (§4.4)."""
    spy = Spy()
    server, client = await wire("tls", socket_dir, controller_pki, dispatch=spy.dispatch)
    try:
        _, writer = await client.connect()
        raw: socket.socket = writer.get_extra_info("socket")
        assert raw.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        writer.close()
    finally:
        await server.stop()
