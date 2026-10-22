"""How long Compose lets a Worker stop before it kills it.

Compose's default is ten seconds. A Worker asked to stop finishes the round
under way, which can be a model call or a command longer than that; killed
first, every Run it held is interrupted and spends a recovery attempt. So the
stop timeout must outlast the Worker's own grace period.
"""

import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[4] / "deploy" / "compose"


def _seconds(duration: str) -> int:
    matched = re.fullmatch(r"(\d+)s", duration)
    assert matched, f"write the stop timeout in seconds, not {duration!r}"
    return int(matched.group(1))


def _default(value: str) -> str:
    """`${NAME:-default}` as Compose reads it with NAME unset."""
    matched = re.fullmatch(r"\$\{\w+:-([^}]*)\}", value)
    return matched.group(1) if matched else value


def test_compose_waits_longer_than_the_worker_grace_period() -> None:
    document: Any = yaml.safe_load((COMPOSE / "compose.yaml").read_text(encoding="utf-8"))
    worker = document["services"]["worker"]
    grace = int(_default(str(document["x-app-env"]["WORKER_SHUTDOWN_GRACE_SECONDS"])))

    assert _seconds(_default(str(worker["stop_grace_period"]))) > grace
