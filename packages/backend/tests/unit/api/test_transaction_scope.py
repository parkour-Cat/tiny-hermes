"""A successful write response must mean its database transaction committed."""

from fastapi.routing import APIRoute
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings

TRANSACTIONAL_DEPENDENCIES = {
    "agent_catalog",
    "auth_service",
    "model_endpoints",
    "run_coordination",
    "secret_service",
    "workspace_service",
}


def test_transaction_dependencies_finish_before_the_response_is_sent() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/database",
        redis_url="",
        s3_endpoint="http://minio:9000",
        s3_bucket="test",
        s3_access_key="test-access-key",
        s3_secret_key="test-secret-key",
        session_cookie_secret="s" * 32,
        bootstrap_token="b" * 32,
    )
    app = create_app(settings=settings)
    found: list[tuple[str, str, str | None]] = []

    for included in app.routes:
        router = getattr(included, "original_router", None)
        for route in getattr(router, "routes", ()):
            if not isinstance(route, APIRoute):
                continue
            for dependency in route.dependant.dependencies:
                name = getattr(dependency.call, "__name__", "")
                if name in TRANSACTIONAL_DEPENDENCIES:
                    found.append((route.path, name, dependency.scope))

    assert found, "no transactional route dependencies were found"
    assert [entry for entry in found if entry[2] != "function"] == []
