"""Where the Controller listens and where its callers look for it (tier B §4.1).

Step 1 of the tier B design: the setting takes a URL, and the only scheme it
accepts yet is the Unix socket every deployment already uses.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from tiny_hermes.sandbox.transport.address import ControllerAddress

COMPOSE = Path(__file__).resolve().parents[5] / "deploy" / "compose" / "compose.yaml"


def test_a_unix_url_names_a_socket_path() -> None:
    address = ControllerAddress.parse("unix:///run/tiny-hermes/controller.sock")

    assert address.path == "/run/tiny-hermes/controller.sock"
    assert str(address) == "unix:///run/tiny-hermes/controller.sock"


def test_plain_tcp_is_refused_because_nothing_would_authenticate_it() -> None:
    # The Controller holds the Docker socket; an unauthenticated way in is a
    # way to root on its host.
    with pytest.raises(ValueError, match="tls://"):
        ControllerAddress.parse("tcp://sandbox-1.internal:7443")


def test_tls_is_not_accepted_until_the_transport_speaks_it() -> None:
    # A setting that parsed and then fell back to the socket would let an
    # operator believe a remote Controller was in use.
    with pytest.raises(ValueError, match="not supported yet"):
        ControllerAddress.parse("tls://sandbox-1.internal:7443")


@pytest.mark.parametrize(
    "value",
    [
        "/run/tiny-hermes/controller.sock",  # no scheme: the old spelling
        "unix://",
        "unix://relative/controller.sock",
        "unix:///run/tiny-hermes/controller.sock?mode=x",
        "unix://host/run/controller.sock",
    ],
)
def test_anything_but_an_absolute_socket_path_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        ControllerAddress.parse(value)


def _environment(service: str) -> dict[str, Any]:
    document: Any = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return dict(document["services"][service].get("environment") or {})


@pytest.mark.parametrize("caller", ["worker", "scheduler"])
def test_compose_points_each_caller_at_the_address_the_controller_listens_on(
    caller: str,
) -> None:
    listen = _environment("controller")["SANDBOX_CONTROLLER_LISTEN"]

    assert ControllerAddress.parse(_environment(caller)["SANDBOX_CONTROLLER_URL"]) == (
        ControllerAddress.parse(listen)
    )


@pytest.mark.parametrize("service", ["controller", "worker", "scheduler"])
def test_compose_no_longer_passes_the_old_spelling(service: str) -> None:
    # Settings refuses the old and the new together, so passing both would
    # stop every process that reads them.
    assert "SANDBOX_CONTROLLER_SOCKET" not in _environment(service)
