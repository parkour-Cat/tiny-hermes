from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import PASSWORD

ENDPOINT: dict[str, Any] = {
    "name": "acme-gpt",
    "kind": "openai_compatible",
    "base_url": "https://models.example.com/v1",
    "model": "acme-large",
    "context_window": 128_000,
    "max_output_tokens": 4_096,
    "usage_quality": "provider",
    "credential_ref": "TINY_HERMES_TEST_MODEL_KEY",
}


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment's side of the bargain: the named variable exists.

    A value that looks nothing like a key, so nothing here could be mistaken for
    one if it ever turned up somewhere it should not.
    """
    monkeypatch.setenv("TINY_HERMES_TEST_MODEL_KEY", "not-a-real-key")


async def become_someone_else(client: TestClient, engine: AsyncEngine) -> str:
    """Swap the signed-in account for one that is not a platform administrator.

    No self-service registration exists yet, so the second local account is
    seeded directly with the same verifier as the bootstrap administrator — the
    pattern the agent API tests already use. Called from inside a test rather
    than resolved as a fixture, because these tests register an endpoint as the
    administrator first and only then want to be somebody else.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, created_at) "
                "VALUES (gen_random_uuid(), 'active', 'Outsider', false, now())"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), id, 'local', 'outsider@example.com', "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now() "
                "FROM users WHERE display_name = 'Outsider'"
            )
        )
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "outsider@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201
    assert login.json()["is_platform_admin"] is False
    return login.cookies["tiny_hermes_csrf"]


def register(client: TestClient, csrf: str, **overrides: Any) -> Any:
    return client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": csrf},
        json={**ENDPOINT, **overrides},
    )


def test_a_platform_administrator_registers_an_endpoint(
    client: TestClient, admin_csrf: str
) -> None:
    created = register(client, admin_csrf)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "acme-gpt"
    assert body["status"] == "active"


async def test_someone_who_is_not_a_platform_administrator_cannot_register_one(
    client: TestClient, admin_csrf: str, engine: AsyncEngine
) -> None:
    """Approving an endpoint is a platform decision, not a tenant's."""
    del admin_csrf
    refused = register(client, await become_someone_else(client, engine))
    assert refused.status_code == 403


def test_a_repeated_name_is_a_conflict_not_a_crash(
    client: TestClient, admin_csrf: str
) -> None:
    assert register(client, admin_csrf).status_code == 201
    clash = register(client, admin_csrf, model="other")
    assert clash.status_code == 409
    assert clash.json()["code"] == "endpoint_name_taken"


def test_an_absent_credential_is_found_at_registration(
    client: TestClient, admin_csrf: str
) -> None:
    """Not inside somebody's Run, hours later, as an unexplained failure."""
    refused = register(client, admin_csrf, credential_ref="TINY_HERMES_NOT_SET_ANYWHERE")
    assert refused.status_code == 422
    assert refused.json()["code"] == "credential_missing"


def test_an_endpoint_may_name_an_active_secret(
    client: TestClient, admin_csrf: str, scope: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/secrets",
        headers=scope,
        json={"name": "model-key", "scope": "platform", "plaintext": "sk-from-secret"},
    )
    assert created.status_code == 201, created.text
    secret_id = created.json()["id"]
    registered = register(client, admin_csrf, credential_ref=secret_id)
    assert registered.status_code == 201, registered.text
    detail = client.get(f"/api/v1/model-endpoints/{registered.json()['id']}")
    assert detail.json()["credential_available"] is True
    assert "credential_ref" not in detail.json()


def test_a_credential_pasted_into_the_reference_is_refused(
    client: TestClient, admin_csrf: str
) -> None:
    refused = register(client, admin_csrf, credential_ref="sk-looks-like-a-real-key")
    assert refused.status_code == 422


def test_estimated_usage_quality_is_refused(client: TestClient, admin_csrf: str) -> None:
    assert register(client, admin_csrf, usage_quality="estimated").status_code == 422


async def test_any_signed_in_user_can_see_what_models_exist(
    client: TestClient, admin_csrf: str, engine: AsyncEngine
) -> None:
    """The draft editor has to offer the list, so an ordinary user can read it."""
    assert register(client, admin_csrf).status_code == 201
    await become_someone_else(client, engine)
    listed = client.get("/api/v1/model-endpoints")
    assert listed.status_code == 200
    assert [entry["name"] for entry in listed.json()] == ["acme-gpt"]


def test_the_list_carries_no_credential_and_no_address(
    client: TestClient, admin_csrf: str
) -> None:
    """Asserted on the absence of keys, not on the absence of values.

    `base_url` is left out because an internal model host is a piece of network
    map, and the console has no use for it.
    """
    assert register(client, admin_csrf).status_code == 201
    entry = client.get("/api/v1/model-endpoints").json()[0]
    assert set(entry) == {
        "id",
        "name",
        "model",
        "context_window",
        "max_output_tokens",
        "usage_quality",
        "status",
    }


def test_the_administrator_detail_shows_the_address_but_never_the_credential(
    client: TestClient, admin_csrf: str
) -> None:
    endpoint_id = register(client, admin_csrf).json()["id"]
    detail = client.get(f"/api/v1/model-endpoints/{endpoint_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["base_url"] == "https://models.example.com/v1"
    # The reference is a variable name rather than a secret, and it is still not
    # returned: `credential_available` is the fact an administrator needs, and
    # naming the variable only tells a reader where to go looking.
    assert body["credential_available"] is True
    assert "credential_ref" not in body
    assert "credential" not in body


async def test_an_ordinary_user_cannot_read_the_detail(
    client: TestClient, admin_csrf: str, engine: AsyncEngine
) -> None:
    endpoint_id = register(client, admin_csrf).json()["id"]
    await become_someone_else(client, engine)
    assert client.get(f"/api/v1/model-endpoints/{endpoint_id}").status_code == 403


def test_disabling_takes_it_out_of_the_list(client: TestClient, admin_csrf: str) -> None:
    endpoint_id = register(client, admin_csrf).json()["id"]
    disabled = client.patch(
        f"/api/v1/model-endpoints/{endpoint_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200
    assert client.get("/api/v1/model-endpoints").json() == []
    # Still addressable by id, because an Agent Version published against it
    # keeps naming it and the publish check has to be able to say why it refused.
    assert client.get(f"/api/v1/model-endpoints/{endpoint_id}").status_code == 200


def test_a_check_against_a_forbidden_address_reports_the_refusal(
    client: TestClient, admin_csrf: str
) -> None:
    """A literal address, so this test needs neither DNS nor a network.

    The cloud metadata service is the address a mistyped `base_url` is most
    likely to reach and the one it must never reach.
    """
    endpoint_id = register(
        client, admin_csrf, base_url="https://169.254.169.254/latest"
    ).json()["id"]
    checked = client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/check", headers={"X-CSRF-Token": admin_csrf}
    )
    assert checked.status_code == 200
    body = checked.json()
    assert body["reachable"] is False
    assert body["refusal"] == "link_local"


def test_a_check_against_loopback_is_refused_too(
    client: TestClient, admin_csrf: str
) -> None:
    endpoint_id = register(client, admin_csrf, base_url="https://127.0.0.1:9/v1").json()["id"]
    checked = client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/check", headers={"X-CSRF-Token": admin_csrf}
    )
    assert checked.json()["refusal"] == "loopback"


def test_a_check_never_returns_what_the_endpoint_said(
    client: TestClient, admin_csrf: str
) -> None:
    """A `base_url` mistyped into an internal service would otherwise make this
    route a way to read it."""
    endpoint_id = register(
        client, admin_csrf, base_url="https://169.254.169.254/latest"
    ).json()["id"]
    body = client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/check", headers={"X-CSRF-Token": admin_csrf}
    ).json()
    assert set(body) <= {"reachable", "refusal", "detail", "elapsed_ms"}


async def test_an_ordinary_user_cannot_run_the_check(
    client: TestClient, admin_csrf: str, engine: AsyncEngine
) -> None:
    endpoint_id = register(client, admin_csrf).json()["id"]
    csrf = await become_someone_else(client, engine)
    refused = client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/check", headers={"X-CSRF-Token": csrf}
    )
    assert refused.status_code == 403


def test_a_write_without_csrf_is_refused(client: TestClient, admin_csrf: str) -> None:
    del admin_csrf
    assert client.post("/api/v1/model-endpoints", json=ENDPOINT).status_code == 403
