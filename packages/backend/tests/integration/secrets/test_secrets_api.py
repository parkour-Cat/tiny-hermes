from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings


def _create(
    client: TestClient,
    scope: dict[str, str],
    *,
    name: str = "openai",
    kind: str = "workspace",
    plaintext: str = "sk-secret-value",
) -> Any:
    return client.post(
        "/api/v1/secrets",
        headers=scope,
        json={"name": name, "scope": kind, "plaintext": plaintext},
    )


async def test_create_returns_a_mask_and_listing_cannot_remember_plaintext(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    created = _create(client, scope)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["mask"] != "sk-secret-value"
    assert "plaintext" not in body
    assert "ciphertext" not in body
    assert body["scope"] == "workspace"

    listed = client.get("/api/v1/secrets", headers=scope)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert "plaintext" not in rows[0]
    assert rows[0]["mask"] == body["mask"]
    assert rows[0]["id"] == body["id"]

    async with engine.connect() as connection:
        columns = (
            await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'secrets'"
                )
            )
        ).scalars().all()
        audits = (
            await connection.execute(
                text("SELECT action FROM audit_events WHERE action = 'secret.created'")
            )
        ).scalars().all()
    assert "plaintext" not in columns
    assert list(audits) == ["secret.created"]


def test_a_duplicate_name_is_a_conflict(client: TestClient, scope: dict[str, str]) -> None:
    assert _create(client, scope).status_code == 201
    clash = _create(client, scope, plaintext="other")
    assert clash.status_code == 409
    assert clash.json()["code"] == "secret_name_taken"


async def test_viewer_cannot_list_or_write(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    assert _create(client, scope).status_code == 201
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE workspace_id = :id"),
            {"id": workspace_id},
        )

    assert client.get("/api/v1/secrets", headers=scope).status_code == 403
    assert _create(client, scope, name="other").status_code == 403


async def test_developer_can_list_names_but_not_write(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    created = _create(client, scope)
    assert created.status_code == 201
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'developer' WHERE workspace_id = :id"),
            {"id": workspace_id},
        )

    listed = client.get("/api/v1/secrets", headers=scope)
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "openai"
    assert "plaintext" not in listed.json()[0]
    assert _create(client, scope, name="other").status_code == 403
    disabled = client.post(
        f"/api/v1/secrets/{created.json()['id']}/disable", headers=scope
    )
    assert disabled.status_code == 403


def test_disable_keeps_the_row_without_plaintext(
    client: TestClient, scope: dict[str, str]
) -> None:
    created = _create(client, scope)
    secret_id = created.json()["id"]
    disabled = client.post(f"/api/v1/secrets/{secret_id}/disable", headers=scope)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert "plaintext" not in disabled.json()


def test_a_platform_administrator_can_write_a_platform_secret(
    client: TestClient, scope: dict[str, str]
) -> None:
    created = _create(client, scope, kind="platform", name="platform-key")
    assert created.status_code == 201
    assert created.json()["workspace_id"] is None
    assert created.json()["scope"] == "platform"


def test_missing_kek_on_write_is_503(
    settings: Settings, admin_csrf: str, workspace_id: str
) -> None:
    del admin_csrf
    without_kek = settings.model_copy(update={"tiny_hermes_kek": ""})
    with TestClient(create_app(settings=without_kek)) as client:
        login = client.post(
            "/api/v1/auth/sessions",
            json={"subject": "admin@example.com", "password": "long-pass-123"},
        )
        assert login.status_code == 201
        refused = client.post(
            "/api/v1/secrets",
            headers={
                "X-Workspace-Id": workspace_id,
                "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
            },
            json={"name": "openai", "scope": "workspace", "plaintext": "value"},
        )
    assert refused.status_code == 503
    assert refused.json()["code"] == "kek_missing"


def test_create_response_shape_has_no_envelope_fields(
    client: TestClient, scope: dict[str, str]
) -> None:
    body = cast(dict[str, Any], _create(client, scope).json())
    assert set(body) == {
        "id",
        "name",
        "scope",
        "workspace_id",
        "status",
        "mask",
        "created_at",
        "updated_at",
    }


TEST_KEK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
NEXT_KEK = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="


def test_rewrap_skips_already_rotated_rows(
    settings: Settings,
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
) -> None:
    assert _create(client, scope, name="one").status_code == 201
    assert _create(client, scope, name="two").status_code == 201
    rotated = settings.model_copy(
        update={
            "tiny_hermes_kek": NEXT_KEK,
            "tiny_hermes_kek_id": "v2",
            "tiny_hermes_previous_kek": TEST_KEK,
            "tiny_hermes_previous_kek_id": "v1",
        }
    )
    with TestClient(create_app(settings=rotated)) as other:
        login = other.post(
            "/api/v1/auth/sessions",
            json={"subject": "admin@example.com", "password": "long-pass-123"},
        )
        headers = {
            "X-Workspace-Id": workspace_id,
            "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
        }
        first = other.post("/api/v1/secrets/rewrap", headers=headers)
        assert first.status_code == 200, first.text
        assert first.json() == {
            "processed": 2,
            "remaining": 0,
            "current_key_id": "v2",
            # Zero, and said out loud: a rotation that reports nothing
            # unrecoverable is the difference between "finished" and
            # "finished, and some ciphertext is gone".
            "unrecoverable": 0,
        }
        second = other.post("/api/v1/secrets/rewrap", headers=headers)
        assert second.json() == {
            "processed": 0,
            "remaining": 0,
            "current_key_id": "v2",
            "unrecoverable": 0,
        }
