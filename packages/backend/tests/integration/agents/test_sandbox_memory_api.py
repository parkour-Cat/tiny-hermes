"""An Agent's sandbox size through the HTTP API (§21.3, v2.22)."""

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from ..conftest import VALID_SPEC


def _draft(client: TestClient, scope: dict[str, str], sandbox: dict[str, Any]) -> Any:
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Boxed", "alias": f"box-{uuid4().hex[:8]}"}
    ).json()
    return client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "sandbox": sandbox}},
    )


def test_a_size_within_the_ceiling_is_saved_and_read_back(
    client: TestClient, scope: dict[str, str]
) -> None:
    saved = _draft(client, scope, {"memory_mb": 256})

    assert saved.status_code == 200
    assert saved.json()["spec"]["sandbox"] == {"memory_mb": 256}


def test_a_size_above_the_ceiling_is_refused_with_both_numbers(
    client: TestClient, scope: dict[str, str]
) -> None:
    """The test settings keep the 4096 MB default ceiling."""
    refused = _draft(client, scope, {"memory_mb": 8192})

    assert refused.status_code == 422
    assert refused.json()["code"] == "sandbox_memory_ceiling_exceeded"
    assert "8192" in refused.json()["detail"]
    assert "4096" in refused.json()["detail"]
    # The console words this itself, in the reader's language, from these.
    assert refused.json()["context"] == {"asked": 8192, "allowed": 4096}


def test_a_size_below_the_floor_is_refused_by_the_document(
    client: TestClient, scope: dict[str, str]
) -> None:
    assert _draft(client, scope, {"memory_mb": 128}).status_code == 422
