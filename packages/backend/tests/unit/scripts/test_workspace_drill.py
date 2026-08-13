"""The workspace drill's decision logic, unit-tested off the stack.

The scenarios themselves need a running Compose stack; what can be wrong
quietly — percentile arithmetic, Docker stat parsing, the safety net around
compose arguments — is provable right here.
"""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parents[5] / "scripts"

_RESTART_SPEC = spec_from_file_location("restart_drill", _SCRIPTS / "restart_drill.py")
assert _RESTART_SPEC and _RESTART_SPEC.loader
restart_drill = module_from_spec(_RESTART_SPEC)
sys.modules["restart_drill"] = restart_drill
_RESTART_SPEC.loader.exec_module(restart_drill)

_SPEC = spec_from_file_location("workspace_drill", _SCRIPTS / "workspace_drill.py")
assert _SPEC and _SPEC.loader
workspace_drill = module_from_spec(_SPEC)
sys.modules["workspace_drill"] = workspace_drill
_SPEC.loader.exec_module(workspace_drill)


def test_percentile_is_stable_at_the_edges() -> None:
    assert workspace_drill._percentile([], 95.0) == 0.0
    assert workspace_drill._percentile([2.0], 95.0) == 2.0
    climbing = [float(value) for value in range(1, 21)]
    assert workspace_drill._percentile(climbing, 50.0) == pytest.approx(10.5)
    assert workspace_drill._percentile(climbing, 95.0) >= 19.0


@pytest.mark.parametrize(
    ("usage", "mib"),
    [
        ("512KiB", 0.5),
        ("64MiB", 64.0),
        ("1.5GiB", 1536.0),
        ("2097152B", 2.0),
    ],
)
def test_docker_memory_usage_parses_every_unit(usage: str, mib: float) -> None:
    assert workspace_drill._to_mib(usage) == pytest.approx(mib)


def test_the_drill_can_never_ask_compose_to_remove_state() -> None:
    """The drill reuses the restart drill's guarded `compose`, so `down -v`
    is refused before Docker hears about it."""
    with pytest.raises(SystemExit):
        restart_drill.compose("down", "-v")


def test_the_gates_are_the_documented_envelopes() -> None:
    """Numbers a reviewer compares against design §16.4, pinned by name."""
    assert workspace_drill.SINGLE_COMMIT_RUNS >= 10
    assert workspace_drill.SINGLE_COMMIT_P95_SECONDS <= 10.0
    assert workspace_drill.LARGE_COMMIT_SECONDS <= 60.0
    assert workspace_drill.WORKER_RSS_LIMIT_MB <= 512.0
