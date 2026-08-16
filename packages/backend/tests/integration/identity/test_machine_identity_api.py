from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def _create_account(
    client: TestClient, scope: dict[str, str], name: str = "ci-bot"
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": name, "role": "developer"},
    )
    assert created.status_code == 201, created.text
    return cast(dict[str, Any], created.json())


def _create_key(
    client: TestClient,
    scope: dict[str, str],
    account_id: str,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    created = client.post(
        f"/api/v1/service-accounts/{account_id}/api-keys",
        headers=scope,
        json={"scopes": scopes or ["runs.read", "runs.write", "runs.control"]},
    )
    assert created.status_code == 201, created.text
    return cast(dict[str, Any], created.json())


async def test_workspace_admin_mints_a_key_whose_plaintext_listing_cannot_remember(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    account = _create_account(client, scope)
    issued = _create_key(client, scope, account["id"])

    assert issued["token"].startswith("thk_")
    assert issued["prefix"] == issued["token"][:8]
    assert "token" in issued

    listed_accounts = client.get("/api/v1/service-accounts", headers=scope)
    assert listed_accounts.status_code == 200
    assert [item["id"] for item in listed_accounts.json()] == [account["id"]]

    listed_keys = client.get(
        f"/api/v1/service-accounts/{account['id']}/api-keys", headers=scope
    )
    assert listed_keys.status_code == 200
    body = listed_keys.json()
    assert len(body) == 1
    assert "token" not in body[0]
    assert body[0]["prefix"] == issued["prefix"]
    assert body[0]["id"] == issued["id"]

    async with engine.connect() as connection:
        digests = (
            await connection.execute(text("SELECT token_digest FROM api_keys"))
        ).scalars().all()
        audits = (
            await connection.execute(
                text(
                    "SELECT action FROM audit_events WHERE action IN "
                    "('service_account.created', 'api_key.created') ORDER BY created_at"
                )
            )
        ).scalars().all()
    assert issued["token"] not in digests
    assert list(audits) == ["service_account.created", "api_key.created"]


async def test_viewer_cannot_create_or_list_keys(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    account = _create_account(client, scope)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE workspace_id = :id"),
            {"id": workspace_id},
        )

    listed = client.get(
        f"/api/v1/service-accounts/{account['id']}/api-keys", headers=scope
    )
    assert listed.status_code == 403

    created = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.write"]},
    )
    assert created.status_code == 403


async def test_developer_can_list_keys_but_not_mint_them(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    account = _create_account(client, scope)
    _create_key(client, scope, account["id"])
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'developer' WHERE workspace_id = :id"),
            {"id": workspace_id},
        )

    listed = client.get(
        f"/api/v1/service-accounts/{account['id']}/api-keys", headers=scope
    )
    assert listed.status_code == 200
    assert "token" not in listed.json()[0]

    minted = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.read"]},
    )
    assert minted.status_code == 403


async def test_disable_and_revoke_are_audited(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    account = _create_account(client, scope)
    issued = _create_key(client, scope, account["id"])

    disabled = client.post(
        f"/api/v1/service-accounts/{account['id']}/disable", headers=scope
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    revoked = client.post(f"/api/v1/api-keys/{issued['id']}/revoke", headers=scope)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert "token" not in revoked.json()

    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                text(
                    "SELECT action FROM audit_events WHERE action IN "
                    "('service_account.disabled', 'api_key.revoked') ORDER BY created_at"
                )
            )
        ).scalars().all()
    assert list(actions) == ["service_account.disabled", "api_key.revoked"]


async def test_a_viewer_account_cannot_be_given_write_scopes(
    client: TestClient, scope: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": "readonly", "role": "viewer"},
    )
    assert created.status_code == 201
    refused = client.post(
        f"/api/v1/service-accounts/{created.json()['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.write"]},
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_api_key_scopes"
