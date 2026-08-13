from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PASSWORD = "long-pass-123"  # noqa: S105 - same local verifier the bootstrap admin uses


async def _seed_user(engine: AsyncEngine, display_name: str, subject: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, created_at) "
                "VALUES (gen_random_uuid(), 'active', :name, false, now())"
            ),
            {"name": display_name},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), id, 'local', :subject, "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now() "
                "FROM users WHERE display_name = :name"
            ),
            {"subject": subject, "name": display_name},
        )


async def test_admin_invites_changes_and_removes_an_existing_user(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    await _seed_user(engine, "Dev", "dev@example.com")

    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "dev@example.com", "role": "developer"},
    )
    assert invited.status_code == 201
    assert invited.json()["subject"] == "dev@example.com"
    assert invited.json()["role"] == "developer"
    user_id = invited.json()["user_id"]

    listed = client.get(f"/api/v1/workspaces/{workspace_id}/members", headers=scope)
    assert listed.status_code == 200
    subjects = {row["subject"] for row in listed.json()}
    assert subjects == {"admin@example.com", "dev@example.com"}

    changed = client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{user_id}",
        headers=scope,
        json={"role": "viewer"},
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "viewer"

    removed = client.delete(
        f"/api/v1/workspaces/{workspace_id}/members/{user_id}", headers=scope
    )
    assert removed.status_code == 204
    remaining = client.get(f"/api/v1/workspaces/{workspace_id}/members", headers=scope)
    assert {row["subject"] for row in remaining.json()} == {"admin@example.com"}


async def test_unknown_email_is_a_named_error_not_a_signup(
    client: TestClient, scope: dict[str, str], workspace_id: str
) -> None:
    missing = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "nobody@example.com", "role": "viewer"},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "user_not_found"
    assert "create" in missing.json()["detail"].lower()


async def test_developer_cannot_invite(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    await _seed_user(engine, "Dev", "dev@example.com")
    await _seed_user(engine, "Other", "other@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "dev@example.com", "role": "developer"},
    )
    assert invited.status_code == 201

    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "dev@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201
    csrf = login.cookies["tiny_hermes_csrf"]
    refused = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers={"X-Workspace-Id": workspace_id, "X-CSRF-Token": csrf},
        json={"email": "other@example.com", "role": "viewer"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "forbidden"
