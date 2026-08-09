import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings


@pytest.mark.asyncio
async def test_workspace_header_and_cross_tenant_boundaries(
    engine: AsyncEngine, database_url: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE audit_events, memberships, workspaces, auth_sessions, "
                "auth_identities, users CASCADE"
            )
        )
    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/0",
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token="a" * 32,
    )

    with TestClient(create_app(settings=settings)) as client:
        assert client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": "a" * 32},
            json={
                "subject": "admin@example.com",
                "display_name": "Admin",
                "password": "long-pass-123",
            },
        ).status_code == 201
        login = client.post(
            "/api/v1/auth/sessions",
            json={"subject": "admin@example.com", "password": "long-pass-123"},
        )
        csrf = login.cookies["tiny_hermes_csrf"]

        missing_csrf = client.post("/api/v1/workspaces", json={"name": "Denied"})
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "csrf_failed"

        first = client.post(
            "/api/v1/workspaces",
            headers={"X-CSRF-Token": csrf},
            json={"name": "A"},
        ).json()
        second = client.post(
            "/api/v1/workspaces",
            headers={"X-CSRF-Token": csrf},
            json={"name": "B"},
        ).json()
        assert {item["name"] for item in client.get("/api/v1/workspaces").json()} == {"A", "B"}

        async with engine.connect() as connection:
            created_audits = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE action = 'workspace.created'"
                    )
                )
            ).scalar_one()
        assert created_audits == 2

        missing = client.get(f"/api/v1/workspaces/{first['id']}/members")
        assert missing.status_code == 400
        assert missing.json()["code"] == "workspace_required"

        mismatch = client.get(
            f"/api/v1/workspaces/{first['id']}/members",
            headers={"X-Workspace-Id": second["id"]},
        )
        assert mismatch.status_code == 403
        assert mismatch.json()["code"] == "workspace_scope_mismatch"
        assert first["name"] not in str(mismatch.json())

        members = client.get(
            f"/api/v1/workspaces/{first['id']}/members",
            headers={"X-Workspace-Id": first["id"]},
        )
        assert members.status_code == 200
        assert members.json()[0]["role"] == "workspace_admin"
