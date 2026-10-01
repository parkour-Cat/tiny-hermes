"""Every long-running service comes back on its own after the Docker daemon does.

On 2026-09-30 sandboxes filled the host's memory, the kernel's OOM killer took
`dockerd`, and every container stopped with it. With no restart policy none of
them came back, Postgres included, until someone ran `docker compose up`
(docs/superpowers/verification/2026-09-30-sandbox-oom.md).
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[4] / "deploy" / "compose" / "compose.yaml"

#: Runs once and exits 0. A policy that restarts it would run the migrations
#: in a loop; nothing needs it again after a daemon restart, because the
#: schema it produced is still there.
ONE_SHOT = {"migrate"}


def _services() -> dict[str, Any]:
    document: Any = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return dict(document["services"])


@pytest.mark.parametrize("service", sorted(set(_services()) - ONE_SHOT))
def test_a_long_running_service_restarts_unless_an_operator_stopped_it(service: str) -> None:
    # `unless-stopped`, not `always`: a service an operator stopped on purpose
    # stays stopped when the daemon comes back.
    assert _services()[service].get("restart") == "unless-stopped"


def test_the_one_shot_migration_is_not_restarted() -> None:
    assert "restart" not in _services()["migrate"]
