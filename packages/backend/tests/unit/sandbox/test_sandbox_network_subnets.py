"""Where each sandbox network's addresses come from (sandbox networks design §3).

Docker's default pools give out about 31 networks on a host, so the controller
picks a /28 from `SANDBOX_NETWORK_POOL` itself, at random, and takes another
when Docker says that one overlaps a network already there.
"""

from ipaddress import IPv4Network
from typing import Any

import pytest
from tiny_hermes.sandbox.application.controller import (
    RefusalReason,
    SandboxController,
    SandboxRefused,
)
from tiny_hermes.sandbox.domain.container_policy import SandboxNetworking
from tiny_hermes.sandbox.infrastructure.docker_engine import SubnetTaken

from .test_controller_workspace import (
    DIGEST,
    LEASE,
    RUN,
    SESSION,
    WORKSPACE,
    FakeEngine,
    FakeStore,
    ScriptedLeases,
)

POOL = IPv4Network("10.249.0.0/16")


class NetworkedEngine(FakeEngine):
    def __init__(self, taken: int) -> None:
        super().__init__()
        self._taken = taken
        self.subnets: list[str] = []

    async def create_network(self, name: str, subnet: str, labels: dict[str, str]) -> None:
        self.calls.append(("create_network", (name, subnet, labels)))
        self.subnets.append(subnet)
        if len(self.subnets) <= self._taken:
            raise SubnetTaken(subnet)

    async def connect(
        self, network: str, container: str, aliases: list[str], *, address: str | None = None
    ) -> None:
        self.calls.append(("connect", (network, container, tuple(aliases))))
        self.proxy_address = address

    async def networks_labelled(self, label: str, value: str | None = None) -> list[Any]:
        self.calls.append(("networks_labelled", (label, value)))
        return []

    async def remove_network_if_present(self, name: str) -> bool:
        self.calls.append(("remove_network", name))
        return True


def _controller(engine: NetworkedEngine) -> SandboxController:
    return SandboxController(
        engine=engine,  # type: ignore[arg-type] - a recording double
        store=FakeStore(),
        approved_digests=(DIGEST,),
        leases=ScriptedLeases(),
        networking=SandboxNetworking(
            proxy_url="http://egress-proxy:3128", proxy_container="the-proxy", pool=POOL
        ),
    )


async def _acquire(controller: SandboxController) -> Any:
    return await controller.acquire(
        run_id=RUN, lease_id=LEASE, workspace_id=WORKSPACE, session_id=SESSION, profile="default"
    )


async def test_a_taken_subnet_is_traded_for_another_from_the_pool() -> None:
    engine = NetworkedEngine(taken=2)

    made = await _acquire(_controller(engine))

    assert len(engine.subnets) == 3
    assert len(set(engine.subnets)) == 3
    for subnet in engine.subnets:
        network = IPv4Network(subnet)
        assert network.prefixlen == 28
        assert network.subnet_of(POOL)  # type: ignore[arg-type]
    [(_, config)] = [call for call in engine.calls if call[0] == "create"]
    assert config.network_mode == f"tiny-hermes-sandbox-{made.sandbox_id}"


async def test_the_proxy_joins_each_network_under_the_name_sandboxes_use() -> None:
    engine = NetworkedEngine(taken=0)

    made = await _acquire(_controller(engine))

    [(_, connected)] = [call for call in engine.calls if call[0] == "connect"]
    assert connected == (f"tiny-hermes-sandbox-{made.sandbox_id}", "the-proxy", ("egress-proxy",))
    # At a fixed address, the subnet's second after the gateway, and the
    # sandbox resolves the proxy's name to it from its hosts (2026-10-24):
    # gVisor does not answer Docker's embedded DNS.
    assert engine.proxy_address == str(IPv4Network(engine.subnets[-1])[2])
    [created] = [config for name, config in engine.calls if name == "create"]
    assert created.extra_hosts == {"egress-proxy": engine.proxy_address}
    order = engine.names()
    assert order.index("create_network") < order.index("connect") < order.index("create")


async def test_a_pool_with_no_room_refuses_rather_than_trying_forever() -> None:
    engine = NetworkedEngine(taken=1000)

    with pytest.raises(SandboxRefused) as refused:
        await _acquire(_controller(engine))

    assert refused.value.reason is RefusalReason.NETWORK_POOL_EXHAUSTED
    assert "create" not in engine.names()
