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


def _headers_for(client: TestClient, subject: str, workspace_id: str) -> dict[str, str]:
    """Sign in as `subject` (seeded with the shared local verifier) and scope
    requests to `workspace_id`."""
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/sessions", json={"subject": subject, "password": PASSWORD}
    )
    assert login.status_code == 201, login.text
    return {"X-Workspace-Id": workspace_id, "X-CSRF-Token": login.cookies["tiny_hermes_csrf"]}


def _member(
    client: TestClient, scope: dict[str, str], workspace_id: str, subject: str, role: str
) -> dict[str, str]:
    """Invite `subject` as `role` (as the administrator) and come back signed
    in as them."""
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": subject, "role": role},
    )
    assert invited.status_code == 201, invited.text
    return _headers_for(client, subject, workspace_id)


async def test_a_viewer_can_ask_what_role_they_have(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """任何成员都可以问自己的角色——那不是别人的信息。

    这一条不能用 `/members` 代替：列成员是一个 viewer 可能被拒绝的动作，而
    「我是谁」不是。控制台需要这个答案来决定不画哪些段。
    """
    await _seed_user(engine, "Vera", "vera@example.com")
    viewer = _member(client, scope, workspace_id, "vera@example.com", "viewer")

    answered = client.get(f"/api/v1/workspaces/{workspace_id}/members/me", headers=viewer)

    assert answered.status_code == 200, answered.text
    assert answered.json() == {"role": "viewer"}


async def test_a_stranger_is_refused_their_own_role(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """不是成员就没有角色可报。回一个空角色会让前端把它当成「某种成员」。"""
    await _seed_user(engine, "Stan", "stan@example.com")
    stranger = _headers_for(client, "stan@example.com", workspace_id)

    refused = client.get(f"/api/v1/workspaces/{workspace_id}/members/me", headers=stranger)

    assert refused.status_code == 403, refused.text


async def test_a_platform_administrator_who_is_not_a_member_is_told_so(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """平台管理员能看见一切，但那不是这个工作空间里的一个角色；伪装成
    `workspace_admin` 会让页面显示一个他并不拥有的身份。

    The bootstrap administrator is the `workspace_admin` of every workspace
    they created, so the case needs one they have left: hand it to a second
    administrator first, because the last one may not leave.
    """
    await _seed_user(engine, "Ada", "ada@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "ada@example.com", "role": "workspace_admin"},
    )
    assert invited.status_code == 201, invited.text
    me = client.get("/api/v1/auth/me").json()["id"]
    left = client.delete(f"/api/v1/workspaces/{workspace_id}/members/{me}", headers=scope)
    assert left.status_code == 204, left.text

    answered = client.get(f"/api/v1/workspaces/{workspace_id}/members/me", headers=scope)

    assert answered.status_code == 200, answered.text
    assert answered.json() == {"role": "platform_admin"}
