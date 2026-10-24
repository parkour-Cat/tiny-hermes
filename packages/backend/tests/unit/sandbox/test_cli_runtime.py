"""The controller starts only on a runtime its Docker has (§1.25, v2.25)."""

import pytest
from tiny_hermes.sandbox.cli import require_runtime


def test_a_runtime_docker_has_is_accepted() -> None:
    require_runtime("runsc", available=("io.containerd.runc.v2", "runc", "runsc"))


def test_a_runtime_docker_lacks_stops_the_controller_naming_what_it_has() -> None:
    """Starting anyway would fail every sandbox, one acquire at a time, with
    an error that names the container and not the setting."""
    with pytest.raises(SystemExit, match=r"SANDBOX_RUNTIME=runsc.*runc"):
        require_runtime("runsc", available=("io.containerd.runc.v2", "runc"))
