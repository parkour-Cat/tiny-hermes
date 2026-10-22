"""What the single-host Compose file exposes, and what it ships with.

`compose.yaml` is the file an operator deploys, and Tier B's worker-host file
extends it. Its defaults are a working local stack; these tests pin that a
default can be a convenience without also being a hole.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"

#: Somebody else's servers, holding this platform's data. None of them is
#: meant to be reached by a browser.
DATASTORES = ("postgres", "redis", "seaweedfs")
BIND = "${DATASTORE_BIND_ADDRESS:-127.0.0.1}:"


def document() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", DATASTORES)
def test_a_datastore_is_published_on_loopback_unless_told_otherwise(name: str) -> None:
    """A bare `5432:5432` listens on every interface of the host.

    Redis has no password and Postgres ships with a known one, so on a host
    with a public address that is the database on the internet. Tier B needs
    them on a private address instead, which is what the variable is for.
    """
    ports = [str(entry) for entry in document()["services"][name]["ports"]]
    assert ports
    assert all(entry.startswith(BIND) for entry in ports), ports


def test_the_environment_reaches_every_process() -> None:
    """Without it every container is `development`: session cookies are never
    marked Secure, and the shipped defaults below pass validation."""
    assert document()["x-base-env"]["ENVIRONMENT"] == "${ENVIRONMENT:-development}"


def test_the_postgres_password_is_one_variable_for_server_and_clients() -> None:
    spec = document()
    password = spec["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert password == "${POSTGRES_PASSWORD:-local-only}"
    assert ":${POSTGRES_PASSWORD:-local-only}@" in spec["x-base-env"]["DATABASE_URL"]


def test_the_object_store_and_its_clients_take_the_same_keys() -> None:
    spec = document()
    server = spec["services"]["seaweedfs"]["environment"]
    app = spec["x-app-env"]
    assert server["AWS_ACCESS_KEY_ID"] == app["S3_ACCESS_KEY"]
    assert server["AWS_SECRET_ACCESS_KEY"] == app["S3_SECRET_KEY"]
