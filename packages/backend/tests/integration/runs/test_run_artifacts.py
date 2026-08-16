from uuid import uuid4

from fastapi.testclient import TestClient


def test_listing_artifacts_for_an_unknown_run_is_generic_not_found(
    client: TestClient, scope: dict[str, str]
) -> None:
    missing = client.get(f"/api/v1/runs/{uuid4()}/artifacts", headers=scope)
    assert missing.status_code == 404
    assert missing.json()["code"] == "run_not_found"


def test_a_run_with_no_artifacts_lists_an_empty_array(
    client: TestClient, scope: dict[str, str], submitted_run: dict[str, object]
) -> None:
    listed = client.get(f"/api/v1/runs/{submitted_run['id']}/artifacts", headers=scope)
    assert listed.status_code == 200
    assert listed.json() == []
