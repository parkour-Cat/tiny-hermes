"""The two Compose files tier B ships, read as an operator would run them.

Tier B design §4.7. A sandbox host runs `compose.yaml` with
`compose.sandbox-host.yaml` on top; a Worker host runs
`compose.worker-host.yaml` alone. What is pinned here is what would otherwise
only be noticed on a second machine: a Worker host that still mounts the
socket volume, a controller port published on every interface, or a container
handed another role's private key.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[4] / "deploy" / "compose"
CERTS = "/etc/tiny-hermes/controller"


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own merge tags, read as the value they carry.

    `!override` replaces an extended service's list instead of appending to
    it, and `!reset` empties one; for what these tests check, the value under
    the tag is what the service ends up with.
    """


def _untagged(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)  # pyright: ignore[reportArgumentType]


_ComposeLoader.add_constructor("!override", _untagged)
_ComposeLoader.add_constructor("!reset", _untagged)


def _document(file: str) -> Any:
    return yaml.load((COMPOSE / file).read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506 - SafeLoader plus two tags


def _service(file: str, name: str) -> dict[str, Any]:
    return dict(_document(file)["services"][name])


def _keys(service: dict[str, Any]) -> list[str]:
    return [str(entry) for entry in service.get("volumes", []) if ".key:" in str(entry)]


@pytest.mark.parametrize(
    ("service", "role"),
    [("controller", "controller"), ("worker", "worker"), ("scheduler", "scheduler")],
)
def test_each_sandbox_host_service_holds_its_own_key_and_no_other(
    service: str, role: str
) -> None:
    definition = _service("compose.sandbox-host.yaml", service)

    assert _keys(definition) == [f"${{CONTROLLER_CERT_DIR:?}}/{role}.key:{CERTS}/{role}.key:ro"]
    environment = definition["environment"]
    assert environment["SANDBOX_CONTROLLER_CERT_FILE"] == f"{CERTS}/{role}.pem"
    assert environment["SANDBOX_CONTROLLER_KEY_FILE"] == f"{CERTS}/{role}.key"
    assert environment["SANDBOX_CONTROLLER_CA_FILE"] == f"{CERTS}/ca.pem"


def test_the_controller_is_published_on_the_private_address_alone() -> None:
    controller = _service("compose.sandbox-host.yaml", "controller")

    # Inside its own container every interface is the container's; what the
    # host exposes is decided by this binding, which must name an address.
    assert controller["environment"]["SANDBOX_CONTROLLER_LISTEN"] == "tls://0.0.0.0:7443"
    assert controller["environment"]["SANDBOX_CONTROLLER_LISTEN_ANY"] == "true"
    assert controller["ports"] == ["${CONTROLLER_PRIVATE_ADDRESS:?}:7443:7443"]


@pytest.mark.parametrize("service", ["worker", "scheduler"])
def test_callers_on_the_sandbox_host_dial_the_controller_by_its_service_name(
    service: str,
) -> None:
    environment = _service("compose.sandbox-host.yaml", service)["environment"]

    assert environment["SANDBOX_CONTROLLER_URL"] == "tls://controller:7443"


def test_a_worker_host_runs_the_worker_and_nothing_else() -> None:
    assert list(_document("compose.worker-host.yaml")["services"]) == ["worker"]


def test_a_worker_host_reaches_the_controller_over_tls_alone() -> None:
    worker = _service("compose.worker-host.yaml", "worker")
    mounts = [str(entry) for entry in worker["volumes"]]

    assert worker["environment"]["SANDBOX_CONTROLLER_URL"] == "${SANDBOX_CONTROLLER_URL:?}"
    assert not any("docker.sock" in mount or "controller-socket" in mount for mount in mounts)
    assert _keys(worker) == [f"${{CONTROLLER_CERT_DIR:?}}/worker.key:{CERTS}/worker.key:ro"]


@pytest.mark.parametrize(
    "variable", ["DATABASE_URL", "REDIS_URL", "S3_ENDPOINT", "SANDBOX_CONTROLLER_URL"]
)
def test_a_worker_host_is_told_where_everything_is_rather_than_guessing(variable: str) -> None:
    # compose.yaml's defaults name services on its own network; on another
    # host they resolve to nothing, so each must be given or the file refuses.
    environment = _service("compose.worker-host.yaml", "worker")["environment"]

    assert environment[variable] == f"${{{variable}:?}}"
