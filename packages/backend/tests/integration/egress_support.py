"""A running egress proxy, for every suite whose subject has to cross one.

Since M2C-1 nothing in this platform reaches the network without the boundary,
so a test that wants a stand-in server needs one of these in the middle. It
lives here rather than in one suite's conftest because three suites need it:
the outbound client's own, the git import's, and the model provider's.

Loopback is relaxed the way it always was — a stand-in has nowhere else to live
— and every suite that relaxes it keeps a strict counterpart so the relaxation
cannot hide a path that skipped the question.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from ipaddress import ip_network

from tiny_hermes.egress.application.proxy import EgressProxy, ProxySettings
from tiny_hermes.egress.domain.decision import AddressPolicy
from tiny_hermes.egress.infrastructure.memory_directory import MemoryScopeDirectory
from tiny_hermes.outbound.domain.address_policy import (
    Address,
    AddressVerdict,
    Network,
    verdict,
)
from tiny_hermes.outbound.domain.scope import OutboundScope

PROXY_TOKEN = "integration-test-token"  # noqa: S105 - a fixed local test value
LOOPBACK: Sequence[Network] = (ip_network("127.0.0.0/8"), ip_network("::1/128"))


def loopback_is_reachable(
    addresses: Sequence[Address], approved: Sequence[Network]
) -> AddressVerdict:
    """The real policy, with loopback permitted so a stand-in is reachable."""
    if addresses and all(entry.is_loopback for entry in addresses):
        chosen = next((entry for entry in addresses if entry.version == 4), addresses[0])
        return AddressVerdict(allowed=True, address=chosen)
    return verdict(addresses, approved)


@dataclass
class ProxyHandle:
    """A running boundary, and the knobs a test needs to move on it."""

    url: str
    directory: MemoryScopeDirectory


@contextlib.asynccontextmanager
async def running_proxy(
    *,
    approved: Sequence[Network] = LOOPBACK,
    policy: AddressPolicy = loopback_is_reachable,
) -> AsyncGenerator[ProxyHandle]:
    """A real egress proxy, because the client can no longer reach past one.

    Every outbound test now runs through this. That is the point of the stage
    rather than an inconvenience: the client's behaviour — redirects, dropped
    credentials, response caps — is only worth asserting on the path it
    actually takes in production.
    """
    listening = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(listening.sockets[0].getsockname()[1])
    listening.close()
    await listening.wait_closed()

    directory = MemoryScopeDirectory(platform=OutboundScope.of(["127.0.0.1", "localhost"]))
    server = EgressProxy(
        directory,
        ProxySettings(
            token=PROXY_TOKEN,
            approved_networks=approved,
            host="127.0.0.1",
            port=port,
            # A stand-in gets whatever port the kernel hands out; the shipped
            # pair is asserted in the egress suite.
            allowed_ports=frozenset(range(1, 65_536)),
        ),
        policy=policy,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve(stop))
    try:
        for _ in range(200):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            del reader
            break
        yield ProxyHandle(url=f"http://127.0.0.1:{port}", directory=directory)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
