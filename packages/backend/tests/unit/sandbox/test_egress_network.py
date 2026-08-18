"""A sandbox has two possible relationships with the network, and no third.

Product design §16.4: 默认无外网. §16.5: its dynamic targets must pass a
network-level boundary. Both hold here — a container is attached either to
nothing at all or to a network that reaches the proxy and nothing else, so
there is no configuration in which it can open a connection nobody checked.

The environment variables are set for the runtimes that read them and relied on
by nothing. That distinction is the point of the last test: a container that
ignored them would still find only one place to send a packet.
"""

from uuid import uuid4

from tiny_hermes.sandbox.domain.container_policy import (
    DEFAULT_PROFILE,
    EgressNetwork,
    container_config,
)

DIGEST = "sha256:" + "a" * 64


def config(egress: EgressNetwork | None = None):
    return container_config(
        digest=DIGEST,
        profile=DEFAULT_PROFILE,
        run_id=uuid4(),
        instance_id=uuid4(),
        workspace_id=uuid4(),
        approved_digests=(DIGEST,),
        egress=egress,
    )


def test_a_deployment_with_no_boundary_gets_a_container_with_no_network() -> None:
    """Not an unguarded network: the absence of a proxy makes a sandbox
    offline, exactly as it was before this stage."""
    assert config().as_docker_kwargs()["network_mode"] == "none"


def test_a_deployment_with_a_boundary_attaches_the_sandbox_to_it() -> None:
    attached = config(EgressNetwork(name="tiny-hermes-egress", proxy_url="http://p:3128"))

    assert attached.as_docker_kwargs()["network_mode"] == "tiny-hermes-egress"


def test_the_proxy_variables_are_set_for_the_runtimes_that_read_them() -> None:
    attached = config(
        EgressNetwork(name="tiny-hermes-egress", proxy_url="http://egress-proxy:3128")
    )

    environment = attached.as_docker_kwargs()["environment"]
    assert environment["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert environment["https_proxy"] == "http://egress-proxy:3128"
    # Loopback stays direct: a tool talking to something it started inside its
    # own container should not hairpin through the boundary.
    assert "127.0.0.1" in environment["NO_PROXY"]
    # And the one the sandbox has always had.
    assert environment["HOME"] == "/workspace/data"


def test_an_offline_sandbox_is_told_about_no_proxy_at_all() -> None:
    environment = config().as_docker_kwargs()["environment"]

    assert environment == {"HOME": "/workspace/data"}


def test_attaching_a_network_changes_nothing_else_about_the_container() -> None:
    """The rest of §16.4 is not traded away for reachability: still no root,
    still a read-only root filesystem, still every capability dropped."""
    offline = config().as_docker_kwargs()
    attached = config(
        EgressNetwork(name="tiny-hermes-egress", proxy_url="http://p:3128")
    ).as_docker_kwargs()

    differences = {
        key
        for key in offline
        if key not in ("network_mode", "environment", "labels", "mounts")
        and offline[key] != attached[key]
    }
    assert differences == set()
    assert attached["read_only"] is True
    assert attached["cap_drop"] == ["ALL"]
    assert attached["security_opt"] == ["no-new-privileges:true"]
