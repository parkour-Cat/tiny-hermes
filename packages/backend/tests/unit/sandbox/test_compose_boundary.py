"""The trust boundary, read out of the Compose file.

Product design §16: `Docker 控制权只授予可信的 sandbox-controller。API、Web、
Agent 沙箱和模型均不得直接访问 Docker socket`. That is enforced by which service
mounts what, which means it is enforced by a YAML file that anybody can edit
while adding an unrelated feature.

So it is asserted here. These tests read the file rather than start the stack:
the claim is about the deployment description, and a running stack would prove
the same thing more slowly and only where Docker is available.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[5]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"

#: Services that run this platform's own code. The datastores are not in scope:
#: postgres and redis are somebody else's images and mount neither socket.
OURS = {"api", "web", "worker", "scheduler", "controller", "migrate"}


def services() -> dict[str, Any]:
    document: Any = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return dict(document["services"])


def mounts(service: dict[str, Any]) -> list[str]:
    return [str(entry) for entry in service.get("volumes", [])]


def test_exactly_one_service_mounts_the_docker_socket() -> None:
    """Counted rather than checked one by one, so a new service that mounts it
    fails here instead of being noticed by whoever reviews the diff."""
    holders = [
        name
        for name, spec in services().items()
        if any("docker.sock" in mount for mount in mounts(spec))
    ]
    assert holders == ["controller"]


@pytest.mark.parametrize("name", sorted(OURS - {"controller"}))
def test_no_other_service_of_ours_can_reach_docker(name: str) -> None:
    assert not [m for m in mounts(services()[name]) if "docker.sock" in m]


def test_only_the_controller_and_its_two_callers_share_the_socket_volume() -> None:
    """The API and the web service have no business talking to the Controller.

    Nothing in a request path should be able to start a container: a Run is the
    only thing that gets a sandbox, and Runs are executed by Workers.
    """
    sharers = {
        name
        for name, spec in services().items()
        if any("controller-socket" in mount for mount in mounts(spec))
    }
    assert sharers == {"controller", "worker", "scheduler"}


def test_the_controller_starts_before_a_worker_waits_on_it() -> None:
    """A Worker that came up first would fail its first tool call on a missing
    socket, which reads as a sandbox bug rather than a startup order."""
    worker = services()["worker"]
    assert "controller" in dict(worker["depends_on"])


def test_the_image_allowlist_is_empty_by_default() -> None:
    """A deployment that has not chosen an image cannot run a tool.

    The alternative — defaulting to a tag — would mean an unconfigured
    deployment silently running whatever was pushed last.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert "SANDBOX_IMAGE_DIGEST: ${SANDBOX_IMAGE_DIGEST:-}" in text


def test_the_controller_is_the_platforms_own_image_not_a_docker_cli() -> None:
    """It talks to the daemon through the API from Python.

    A controller built on the docker CLI would need a shell to drive it, which
    is the host execution the process ban exists to prevent.
    """
    controller = services()["controller"]
    assert controller["command"] == ["/app/.venv/bin/tiny-hermes-controller"]
    assert controller["build"]["dockerfile"] == "apps/api/Dockerfile"
