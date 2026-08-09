from fastapi.testclient import TestClient
from tiny_hermes.api.app import create_app


def test_liveness_does_not_depend_on_external_services() -> None:
    response = TestClient(create_app(readiness=lambda: False)).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_dependency_failure() -> None:
    response = TestClient(create_app(readiness=lambda: False)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_reports_success() -> None:
    response = TestClient(create_app(readiness=lambda: True)).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
