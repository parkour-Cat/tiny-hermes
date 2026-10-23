"""One network per sandbox (product design §1.21.4, v2.21), asked of the kernel.

Before this, every networked sandbox shared one `internal` bridge, and
`internal` removes the way out, not the way across: one Run could connect to a
port another Run had opened in its own sandbox.

The proxy here is a stand-in container listening on 3128. The runtime image
has perl in it (the Debian base needs it), which is enough to listen with, so
nothing is added to the image for these tests.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from ipaddress import IPv4Network
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.sandbox.application.controller import AcquireResult, SandboxController
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import SandboxNetworking
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

from .conftest import LABEL, LISTEN, RecordingAudit, StubLeases

WORKSPACE = uuid4()
LEASE = uuid4()
PUBLIC = "93.184.216.34"
#: Not one Docker hands out by default, and not the Compose project's.
POOL = IPv4Network("10.249.0.0/16")

@pytest.fixture
def runs(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> Callable[[], UUID]:
    """Each call a new Run of one Agent, in a Session of its own: the
    controller writes down a sandbox's address, and that row belongs to a Run."""

    def made() -> UUID:
        session = client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
        ).json()["id"]
        created = client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": str(uuid4())},
            json={"session_id": session, "input": "hello"},
        )
        assert created.status_code == 201, created.text
        return UUID(created.json()["id"])

    return made


@pytest.fixture
async def networked(
    engine: AsyncEngine,
    empty_database: None,
    docker_client: Any,
    image_digest: str,
    leases: StubLeases,
    audit: RecordingAudit,
    proxy: Any,
) -> AsyncIterator[SandboxController]:
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    async with sessions() as session:
        yield SandboxController(
            engine=DockerEngine(docker_client, extra_labels={LABEL: "1"}),
            store=SqlSandboxStore(session),
            approved_digests=(image_digest,),
            leases=leases,
            audit=audit,
            networking=SandboxNetworking(
                proxy_url="http://egress-proxy:3128", proxy_container=proxy.name, pool=POOL
            ),
        )
        await session.commit()
    for leftover in docker_client.containers.list(
        all=True, filters={"label": "tiny-hermes.run"}
    ):
        if leftover.labels.get(LABEL) == "1":
            leftover.remove(force=True)
    for network in docker_client.networks.list(filters={"label": LABEL}):
        network.reload()
        for attached in network.attrs.get("Containers", {}):
            network.disconnect(attached, force=True)
        network.remove()


async def _acquired(controller: SandboxController, run_id: UUID) -> AcquireResult:
    return await controller.acquire(
        run_id=run_id, lease_id=LEASE, workspace_id=WORKSPACE, profile="default"
    )


async def _run(controller: SandboxController, docker_client: Any, run_id: UUID) -> Any:
    made = await _acquired(controller, run_id)
    instance = await controller.inspect(run_id=run_id, sandbox_id=made.sandbox_id)
    return made, docker_client.containers.get(instance.container_id)


def _networks_of(container: Any) -> dict[str, Any]:
    container.reload()
    return dict(container.attrs["NetworkSettings"]["Networks"])


async def _reaches(container: Any, host: str, port: int) -> bool:
    command = f"timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}' && echo open || echo shut"
    result = await asyncio.to_thread(container.exec_run, ["bash", "-c", command])
    return b"open" in result.output


def _listen_in(container: Any, port: int) -> None:
    container.exec_run(["perl", "-e", LISTEN.format(port=port)], detach=True)


async def test_each_sandbox_gets_a_network_of_its_own_with_only_the_proxy_on_it(
    networked: SandboxController, docker_client: Any, proxy: Any, runs: Callable[[], UUID]
) -> None:
    _, first = await _run(networked, docker_client, runs())
    _, second = await _run(networked, docker_client, runs())

    [first_network] = _networks_of(first)
    [second_network] = _networks_of(second)
    assert first_network != second_network
    for name, sandbox in ((first_network, first), (second_network, second)):
        attached = docker_client.networks.get(name)
        attached.reload()
        assert set(attached.attrs["Containers"]) == {sandbox.id, proxy.id}
        assert attached.attrs["Internal"] is True


async def test_a_sandbox_cannot_reach_a_port_another_sandbox_opened(
    networked: SandboxController, docker_client: Any, runs: Callable[[], UUID]
) -> None:
    """The point of this change. The control is the listener answering inside
    its own sandbox, so a closed answer from the other one means unreachable,
    not nothing listening."""
    _, ours = await _run(networked, docker_client, runs())
    _, theirs = await _run(networked, docker_client, runs())
    _listen_in(theirs, 8080)
    await asyncio.sleep(1)
    [(_, settings)] = _networks_of(theirs).items()
    their_address = settings["IPAddress"]

    assert await _reaches(theirs, "127.0.0.1", 8080) is True
    assert await _reaches(ours, their_address, 8080) is False


async def test_a_sandbox_reaches_the_proxy_by_the_name_in_its_proxy_url(
    networked: SandboxController, docker_client: Any, runs: Callable[[], UUID]
) -> None:
    _, sandbox = await _run(networked, docker_client, runs())

    assert await _reaches(sandbox, "egress-proxy", 3128) is True


async def test_a_sandbox_on_its_own_network_still_cannot_reach_the_internet(
    networked: SandboxController, docker_client: Any, runs: Callable[[], UUID]
) -> None:
    _, sandbox = await _run(networked, docker_client, runs())

    assert await _reaches(sandbox, PUBLIC, 80) is False


async def test_destroying_a_sandbox_takes_its_network_away(
    networked: SandboxController, docker_client: Any, proxy: Any, runs: Callable[[], UUID]
) -> None:
    run_id = runs()
    made, sandbox = await _run(networked, docker_client, run_id)
    [name] = _networks_of(sandbox)

    await networked.destroy(run_id=run_id, lease_id=LEASE, sandbox_id=made.sandbox_id)

    assert docker_client.networks.list(names=[name]) == []
    assert name not in _networks_of(proxy)


async def test_the_host_sweep_removes_a_network_nothing_records(
    networked: SandboxController, docker_client: Any, runs: Callable[[], UUID]
) -> None:
    """What an acquire that died between the network and the row leaves."""
    stray = docker_client.networks.create(
        f"tiny-hermes-sandbox-{uuid4()}",
        driver="bridge",
        internal=True,
        labels={
            LABEL: "1",
            "tiny-hermes.run": str(uuid4()),
            "tiny-hermes.instance": str(uuid4()),
        },
    )

    await networked.sweep()

    assert docker_client.networks.list(names=[stray.name]) == []


async def test_a_command_in_a_networked_sandbox_still_runs(
    networked: SandboxController, docker_client: Any, runs: Callable[[], UUID]
) -> None:
    run_id = runs()
    made = await _acquired(networked, run_id)

    result = await networked.execute(
        run_id=run_id,
        lease_id=LEASE,
        sandbox_id=made.sandbox_id,
        command=SandboxCommand(
            argv=["echo", "fine"], cwd="/workspace/data", timeout_seconds=10, output_limit=100
        ),
    )

    assert result.output.strip() == "fine"
