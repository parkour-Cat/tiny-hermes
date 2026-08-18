"""What a real container can and cannot reach, asked of the kernel.

The unit tests say what the platform *asks* Docker for. These ask the daemon
what it did, because the interesting claims here are network claims and a
configuration that reads correctly can still leave a route open.

What is asserted here is the *negative* half, twice: a sandbox with no network
reaches nothing, and a sandbox attached to the egress network still reaches
nothing on the internet. Those are the claims the whole arrangement rests on —
attaching a container to a network is only safe because that network goes
nowhere.

The positive half — a sandbox reaching the proxy through that network — is not
here, and the reason is worth stating. It needs something listening on the
network, and the runtime image ships no `curl`, no `nc` and no interpreter to
build one out of: it contains a shell and the file utilities and nothing else,
deliberately. Putting a network tool in it to make a test easier would put that
tool in every Agent's hands. The reachable half is proven where both ends
actually exist, which is Compose — see `docs/development.md`.

The probe is bash's `/dev/tcp`, a shell builtin, so nothing is added to the
image for it.
"""

import uuid
from typing import Any

import pytest
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import (
    DEFAULT_PROFILE,
    EgressNetwork,
    container_config,
)
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine

LABEL = "tiny-hermes.test"
#: A public address, used as a literal so the probe needs no DNS: what is being
#: asked is whether a route exists, and a name would answer a different
#: question when resolution itself is what fails.
PUBLIC = "93.184.216.34"


@pytest.fixture
def engine(docker_client: Any) -> DockerEngine:
    return DockerEngine(docker_client, extra_labels={LABEL: "1"})


@pytest.fixture
def egress_network(docker_client: Any) -> Any:
    """An `internal` bridge: Docker's way of saying it has no gateway.

    The same declaration `deploy/compose/compose.yaml` makes. Nothing attached
    to it can route anywhere, which is what makes the proxy the only place a
    sandbox can send a packet.
    """
    network = docker_client.networks.create(
        f"tiny-hermes-test-egress-{uuid.uuid4().hex[:8]}",
        driver="bridge",
        internal=True,
        labels={LABEL: "1"},
    )
    yield network
    network.remove()


async def _container(
    engine: DockerEngine,
    docker_client: Any,
    image_digest: str,
    egress: EgressNetwork | None,
) -> Any:
    run_id = uuid.uuid4()
    name = f"tiny-hermes-data-{run_id}"
    docker_client.volumes.create(name=name, labels={LABEL: "1"})
    config = container_config(
        digest=image_digest,
        profile=DEFAULT_PROFILE,
        run_id=run_id,
        instance_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        approved_digests=(image_digest,),
        egress=egress,
    )
    container_id = await engine.create(config)
    return docker_client.containers.get(container_id)


async def _reaches(engine: DockerEngine, container_id: str, host: str, port: int) -> bool:
    """Whether a TCP connection opens, asked with a shell builtin.

    `timeout` bounds it so an unreachable address fails in a second rather than
    holding the test for the kernel's retry budget.
    """
    result = await engine.execute(
        container_id,
        SandboxCommand(
            argv=[
                "bash",
                "-c",
                f"timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}' && echo open || echo shut",
            ],
            cwd="/workspace/data",
            timeout_seconds=20,
            output_limit=4096,
        ),
    )
    return "open" in result.output


async def test_a_sandbox_with_no_network_reaches_nothing(
    engine: DockerEngine, docker_client: Any, image_digest: str
) -> None:
    """§16.4's default, and what a deployment with no proxy still gets."""
    container = await _container(engine, docker_client, image_digest, None)
    try:
        assert await _reaches(engine, container.id, PUBLIC, 80) is False
    finally:
        container.remove(force=True)


async def test_a_sandbox_on_the_egress_network_still_cannot_reach_the_internet(
    engine: DockerEngine, docker_client: Any, image_digest: str, egress_network: Any
) -> None:
    """The claim the whole arrangement rests on.

    Attaching a sandbox to a network is only safe because that network goes
    nowhere. If this ever passes, a sandbox has a route the boundary never saw
    and every scope in the platform is advisory.
    """
    container = await _container(
        engine,
        docker_client,
        image_digest,
        EgressNetwork(name=egress_network.name, proxy_url="http://egress-proxy:3128"),
    )
    try:
        assert await _reaches(engine, container.id, PUBLIC, 80) is False
    finally:
        container.remove(force=True)


async def test_the_address_docker_hands_out_is_the_one_the_platform_writes_down(
    engine: DockerEngine, docker_client: Any, image_digest: str, egress_network: Any
) -> None:
    """The Controller reads it back rather than assuming it, because the proxy
    compares against the address that actually arrives."""
    container = await _container(
        engine,
        docker_client,
        image_digest,
        EgressNetwork(name=egress_network.name, proxy_url="http://egress-proxy:3128"),
    )
    try:
        found = await engine.address_of(container.id)
        container.reload()
        expected = container.attrs["NetworkSettings"]["Networks"][egress_network.name][
            "IPAddress"
        ]
        assert found == expected
    finally:
        container.remove(force=True)


async def test_a_container_with_no_network_has_no_address_to_write_down(
    engine: DockerEngine, docker_client: Any, image_digest: str
) -> None:
    """So it is registered as nobody, and nothing will ever ask who it is."""
    container = await _container(engine, docker_client, image_digest, None)
    try:
        assert await engine.address_of(container.id) is None
    finally:
        container.remove(force=True)
