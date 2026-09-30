"""The capacity knobs an operator is told to turn reach the processes that read them.

`Settings` reads these from the environment, but a Compose deployment only
sees what `deploy/compose/compose.yaml` passes into each container. On
2026-09-29 two of them — the model-call ceiling that bounds a long task and
the time slice that bounds a queued Run's wait — existed in `Settings` and in
no container, so the capacity plan's advice could not be followed without
editing the file.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[4] / "deploy" / "compose" / "compose.yaml"

#: (variable, the service that reads it)
KNOBS = [
    # Checked when an Agent is published, which the api does.
    ("AGENT_MAX_MODEL_CALLS", "api"),
    ("AGENT_MAX_TOOL_CALLS", "api"),
    # Read by the Worker that runs the slice.
    ("WORKER_MAX_SLICE_SECONDS", "worker"),
    ("WORKER_CONCURRENCY", "worker"),
    ("SANDBOX_IDLE_TTL_SECONDS", "worker"),
]


def _environment(service: str) -> dict[str, Any]:
    document: Any = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return dict(document["services"][service].get("environment") or {})


@pytest.mark.parametrize(("variable", "service"), KNOBS)
def test_a_capacity_knob_reaches_the_service_that_reads_it(variable: str, service: str) -> None:
    assert variable in _environment(service)
