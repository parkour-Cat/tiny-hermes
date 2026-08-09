import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.shared.config import Settings


@pytest.mark.asyncio
async def test_bootstrap_login_me_and_logout(engine: AsyncEngine, database_url: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE audit_events, auth_sessions, auth_identities, users CASCADE")
        )
    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/0",
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token="a" * 32,
    )

    with TestClient(create_app(settings=settings)) as api_client:
        denied_bootstrap = api_client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": "b" * 32},
            json={
                "subject": "admin@example.com",
                "display_name": "Admin",
                "password": "long-pass-123",
            },
        )
        assert denied_bootstrap.status_code == 403

        bootstrap = api_client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": "a" * 32},
            json={
                "subject": "admin@example.com",
                "display_name": "Admin",
                "password": "long-pass-123",
            },
        )
        assert bootstrap.status_code == 201
        assert bootstrap.json()["is_platform_admin"] is True

        closed = api_client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": "a" * 32},
            json={
                "subject": "second@example.com",
                "display_name": "Second",
                "password": "long-pass-456",
            },
        )
        assert closed.status_code == 409
        assert closed.json()["code"] == "bootstrap_closed"
        assert closed.headers["content-type"].startswith("application/problem+json")
        assert closed.headers["X-Request-Id"] == closed.json()["request_id"]

        denied_login = api_client.post(
            "/api/v1/auth/sessions",
            json={"subject": "admin@example.com", "password": "wrong-password"},
        )
        assert denied_login.status_code == 401

        login = api_client.post(
            "/api/v1/auth/sessions",
            json={"subject": "admin@example.com", "password": "long-pass-123"},
        )
        assert login.status_code == 201
        assert login.cookies.get("tiny_hermes_session")
        csrf = login.cookies["tiny_hermes_csrf"]

        me = api_client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["subject"] == "admin@example.com"

        missing_csrf = api_client.delete("/api/v1/auth/sessions/current")
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "csrf_failed"
        assert api_client.get("/api/v1/auth/me").status_code == 200

        logout = api_client.delete(
            "/api/v1/auth/sessions/current", headers={"X-CSRF-Token": csrf}
        )
        assert logout.status_code == 204
        assert api_client.get("/api/v1/auth/me").status_code == 401

    async with engine.connect() as connection:
        actions = set((await connection.scalars(select(AuditEventRow.action))).all())
    assert {
        "identity.bootstrap_failed",
        "identity.bootstrap_succeeded",
        "identity.login_failed",
        "identity.login_succeeded",
        "identity.logout_succeeded",
    } <= actions
