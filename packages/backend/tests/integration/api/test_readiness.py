from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tiny_hermes.api.app import create_app
from tiny_hermes.api.health import DatabaseReadinessProbe
from tiny_hermes.shared.config import Settings


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/0",
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token="test-bootstrap-token-with-32-characters",
    )


def test_readiness_is_503_when_database_probe_fails() -> None:
    settings = _settings(
        "postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:1/unreachable"
    )

    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}


async def test_reachable_empty_schema_is_reported_as_migration_behind(
    engine: AsyncEngine, database_url: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS readiness_probe_empty CASCADE"))
        await connection.execute(text("CREATE SCHEMA readiness_probe_empty"))
    probe_engine = create_async_engine(
        database_url,
        connect_args={"server_settings": {"search_path": "readiness_probe_empty"}},
    )
    probe = DatabaseReadinessProbe(lambda: probe_engine, "expected_revision")

    try:
        checks = await probe()
    finally:
        await probe_engine.dispose()
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA readiness_probe_empty CASCADE"))

    assert checks == {"database": "ok", "migration": "behind"}


async def test_readiness_is_503_when_schema_is_behind(
    engine: AsyncEngine, database_url: str
) -> None:
    async with engine.begin() as connection:
        current_revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        await connection.execute(text("UPDATE alembic_version SET version_num = 'old_revision'"))

    try:
        with TestClient(create_app(settings=_settings(database_url))) as client:
            response = client.get("/health/ready")
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": current_revision},
            )

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "migration": "behind"},
    }
