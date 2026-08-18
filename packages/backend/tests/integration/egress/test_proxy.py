"""The proxy as a running server, with a real socket on both sides.

The unit tests say what the decision is. These say the process makes it: a real
listener, a real target, real bytes.

To reach a server on this machine at all they relax the address policy in one
named way — loopback becomes reachable — exactly as
`tests/integration/outbound/test_safe_client.py` does, and for the same reason:
loopback is refused by design and a stand-in server has nowhere else to live.
The relaxation cannot hide a proxy that skipped the question, because
`test_the_real_address_policy_is_still_asked` builds one with the real policy
and watches the same request fail.

No database: the directory is the in-memory one, which is also what a
single-tenant deployment runs.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from ipaddress import ip_address, ip_network
from uuid import uuid4

import pytest
from tiny_hermes.egress.application.proxy import SCOPE_HEADER, EgressProxy, ProxySettings
from tiny_hermes.egress.infrastructure.memory_directory import MemoryScopeDirectory
from tiny_hermes.outbound.domain.address_policy import (
    Address,
    AddressVerdict,
    Network,
    verdict,
)
from tiny_hermes.outbound.domain.scope import OutboundScope

TOKEN = "platform-process-token"  # noqa: S105 - a fixed local test value
#: `localhost` answers with both families on this host, and the address policy
#: refuses a name whose answers are not *all* approved — so approving one
#: family and not the other would refuse every test here for the right reason
#: and teach nothing.
LOOPBACK: Sequence[Network] = (ip_network("127.0.0.0/8"), ip_network("::1/128"))
BODY = b"the target answered"


def loopback_is_reachable(
    addresses: Sequence[Address], approved: Sequence[Network]
) -> AddressVerdict:
    """The real policy, with loopback permitted so a local target is usable.

    It picks the IPv4 answer when there is one: `localhost` answers with both
    families here and the stand-in binds `127.0.0.1`, so choosing the first
    answer would prove nothing about the proxy and everything about which
    family this host lists first.
    """
    if addresses and all(entry.is_loopback for entry in addresses):
        chosen = next(
            (entry for entry in addresses if entry.version == 4), addresses[0]
        )
        return AddressVerdict(allowed=True, address=chosen)
    return verdict(addresses, approved)


class Target:
    """A minimal HTTP server that answers one line, and remembers the head."""

    def __init__(self) -> None:
        self.heads: list[bytes] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        self.heads.append(head)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(BODY)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + BODY
        )
        await writer.drain()
        writer.close()


class RunningProxy:
    def __init__(self, proxy: EgressProxy, port: int, stop: asyncio.Event) -> None:
        self.proxy = proxy
        self.port = port
        self._stop = stop
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self.proxy.serve(self._stop))
        # `serve` binds before it waits, and the bind is what the client needs.
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            del reader
            return
        raise RuntimeError("the proxy never accepted a connection")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5)


async def _free_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


async def _proxy(
    directory: MemoryScopeDirectory,
    *,
    approved: Sequence[Network] = LOOPBACK,
    resolve: object | None = None,
    policy: object = loopback_is_reachable,
    ports: frozenset[int] | None = None,
) -> RunningProxy:
    port = await _free_port()
    settings = ProxySettings(
        token=TOKEN,
        approved_networks=approved,
        host="127.0.0.1",
        port=port,
        # A stand-in server gets whatever port the kernel hands out, so the
        # default pair would refuse every test here. The default itself is
        # asserted in the unit tests and in the one test below that keeps it.
        allowed_ports=ports if ports is not None else frozenset(range(1024, 65_536)),
    )
    extra: dict[str, object] = {"policy": policy}
    if resolve is not None:
        extra["resolve"] = resolve
    proxy = EgressProxy(directory, settings, **extra)  # type: ignore[arg-type]
    running = RunningProxy(proxy, port, asyncio.Event())
    await running.start()
    return running


async def _through(
    proxy: RunningProxy,
    target_port: int,
    *,
    token: str | None = TOKEN,
    scope_header: str | None = None,
    host: str = "localhost",
    path: str = "/thing",
) -> tuple[int, bytes]:
    """One absolute-form request through the proxy. Returns status and body."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    lines = [f"GET http://{host}:{target_port}{path} HTTP/1.1", f"Host: {host}"]
    if token is not None:
        lines.append(f"Proxy-Authorization: Bearer {token}")
    if scope_header is not None:
        lines.append(f"{SCOPE_HEADER}: {scope_header}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(65_536), timeout=5)
    writer.close()
    status = int(answer.split(b" ")[1])
    _, _, body = answer.partition(b"\r\n\r\n")
    return status, body


@pytest.fixture
async def target() -> AsyncIterator[Target]:
    server = Target()
    await server.start()
    yield server
    await server.stop()


# -- the happy path ---------------------------------------------------------


async def test_an_approved_target_is_reached_and_the_answer_comes_back(
    target: Target,
) -> None:
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory)
    try:
        status, body = await _through(proxy, target.port)
    finally:
        await proxy.stop()

    assert status == 200
    assert body == BODY
    # Rewritten to origin form on the way out, and the proxy's own credential
    # did not travel with it.
    assert target.heads[0].startswith(b"GET /thing HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in target.heads[0]


async def test_the_layers_a_caller_names_narrow_what_it_may_reach(
    target: Target,
) -> None:
    workspace = uuid4()
    directory = MemoryScopeDirectory(
        platform=OutboundScope.of(["localhost", "api.example.com"]),
        workspaces={workspace: OutboundScope.of(["api.example.com"])},
    )
    proxy = await _proxy(directory)
    try:
        without = await _through(proxy, target.port)
        narrowed = await _through(proxy, target.port, scope_header=f"workspace={workspace}")
    finally:
        await proxy.stop()

    # The platform layer alone approves localhost; adding the workspace layer,
    # which does not, closes it. A layer can only narrow.
    assert without[0] == 200
    assert narrowed[0] == 403


async def test_an_unknown_workspace_id_closes_the_chain_rather_than_dropping_out(
    target: Target,
) -> None:
    """Absent and unknown are different, and the difference is a security rule:
    an id nobody recognizes must not fall back to the layer above it."""
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory)
    try:
        status, body = await _through(proxy, target.port, scope_header=f"workspace={uuid4()}")
    finally:
        await proxy.stop()

    assert status == 403
    assert b"scope_empty" in body


# -- refusals ---------------------------------------------------------------


async def test_a_caller_with_no_token_and_no_sandbox_entry_is_refused(
    target: Target,
) -> None:
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory)
    try:
        status, body = await _through(proxy, target.port, token=None)
    finally:
        await proxy.stop()

    assert status == 407
    assert b"unknown_caller" in body
    # Refused before the target was touched: the connection never happened.
    assert target.heads == []


async def test_a_wrong_token_is_refused_as_an_unknown_caller(target: Target) -> None:
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory)
    try:
        status, _ = await _through(proxy, target.port, token="not-the-token")
    finally:
        await proxy.stop()

    assert status == 407
    assert target.heads == []


async def test_a_target_nobody_approved_is_refused_with_a_reason(target: Target) -> None:
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["api.example.com"]))
    proxy = await _proxy(directory)
    try:
        status, body = await _through(proxy, target.port)
    finally:
        await proxy.stop()

    assert status == 403
    # The reason travels, so a misconfigured scope is readable rather than
    # something an operator has to reproduce with a packet capture.
    assert b"target_not_in_scope" in body
    assert target.heads == []


async def test_the_real_address_policy_is_still_asked(target: Target) -> None:
    """The test that keeps every relaxed one honest.

    Same request, same scope, same proxy — built with the policy this platform
    actually ships. Loopback is refused ahead of any approval, so a request the
    other tests complete fails here, which is only possible if the question is
    genuinely asked on the path.
    """
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory, approved=[], policy=verdict)
    try:
        status, body = await _through(proxy, target.port)
    finally:
        await proxy.stop()

    assert status == 403
    assert b"loopback" in body
    assert target.heads == []


async def test_a_port_the_platform_does_not_serve_is_refused(target: Target) -> None:
    """An approved host is not thereby an approved database server."""
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    # The shipped pair, so this test is about the default rather than about
    # whatever the other tests widened it to.
    proxy = await _proxy(directory, ports=frozenset({80, 443}))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(
            b"CONNECT localhost:5432 HTTP/1.1\r\n"
            b"Proxy-Authorization: Bearer " + TOKEN.encode() + b"\r\n\r\n"
        )
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
    finally:
        await proxy.stop()

    assert b"403" in answer.split(b"\r\n")[0]
    assert b"port_not_allowed" in answer


async def test_a_head_that_is_not_a_request_is_answered_rather_than_dropped() -> None:
    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"GET /origin-form HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
    finally:
        await proxy.stop()

    assert b"400" in answer.split(b"\r\n")[0]


# -- pinning ----------------------------------------------------------------


async def test_the_connection_goes_to_the_address_that_was_checked(
    target: Target,
) -> None:
    """DNS rebinding, expressed as a resolver that changes its mind.

    The first answer is what the policy sees. If the proxy asked again on its
    way to the socket it would get the metadata address; because it connects to
    the literal it checked, the second answer never happens.
    """
    answers = [[ip_address("127.0.0.1")], [ip_address("169.254.169.254")]]

    def resolve(host: str, port: int) -> list[Address]:
        del host, port
        return answers.pop(0) if len(answers) > 1 else answers[0]

    directory = MemoryScopeDirectory(platform=OutboundScope.of(["localhost"]))
    proxy = await _proxy(directory, resolve=resolve)
    try:
        status, body = await _through(proxy, target.port)
    finally:
        await proxy.stop()

    assert status == 200
    assert body == BODY


# -- the sandbox's identity -------------------------------------------------


async def test_a_sandbox_is_identified_by_where_it_came_from(target: Target) -> None:
    """It presents nothing, because a process inside a container that holds a
    credential is a process that can lend it."""
    workspace = uuid4()
    run = uuid4()
    directory = MemoryScopeDirectory(
        platform=OutboundScope.of(["localhost"]),
        workspaces={workspace: OutboundScope.of(["localhost"])},
        runs={run: OutboundScope.of(["localhost"])},
    )
    directory.register_sandbox(
        ip_address("127.0.0.1"), workspace_id=workspace, run_id=run
    )
    proxy = await _proxy(directory)
    try:
        status, body = await _through(proxy, target.port, token=None)
    finally:
        await proxy.stop()

    assert status == 200
    assert body == BODY


async def test_a_sandbox_gets_its_run_s_scope_and_not_the_platform_s(
    target: Target,
) -> None:
    workspace = uuid4()
    run = uuid4()
    directory = MemoryScopeDirectory(
        platform=OutboundScope.of(["localhost"]),
        workspaces={workspace: OutboundScope.of(["localhost"])},
        runs={run: OutboundScope.of(["api.example.com"])},
    )
    directory.register_sandbox(
        ip_address("127.0.0.1"), workspace_id=workspace, run_id=run
    )
    proxy = await _proxy(directory)
    try:
        status, body = await _through(proxy, target.port, token=None)
    finally:
        await proxy.stop()

    assert status == 403
    assert b"scope_empty" in body or b"target_not_in_scope" in body
    assert target.heads == []
