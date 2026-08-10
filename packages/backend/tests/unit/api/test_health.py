from fastapi.testclient import TestClient
from tiny_hermes.api.app import create_app


async def failed_readiness() -> dict[str, str]:
    return {"database": "failed"}


async def successful_readiness() -> dict[str, str]:
    return {"database": "ok", "migration": "current"}


def test_liveness_does_not_depend_on_external_services() -> None:
    response = TestClient(create_app(readiness=failed_readiness)).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_dependency_failure() -> None:
    response = TestClient(create_app(readiness=failed_readiness)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}


def test_readiness_reports_success() -> None:
    response = TestClient(create_app(readiness=successful_readiness)).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migration": "current"},
    }
