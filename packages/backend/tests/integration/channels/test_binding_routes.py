"""Creating the row that makes a channel exist.

Everything else in this module — signature verification, decryption, the
exactly-once claim, ingestion into a Run, the blocked notice — has worked
since the Feishu channel landed, and until this route the **only** thing
that ever inserted a `channel_bindings` row was a test. The whole transport
was reachable by writing a row into Postgres by hand and no other way.
§20.1 names Channels as an M3 navigation entry; there was nothing for it to
navigate to.

§4.6's line is `密钥、安全策略与渠道`: an administrator manages the
metadata and never sees plaintext, a developer may *use* an authorized
binding, a viewer gets nothing at all — not even the list. That last one is
stricter than most of this console and is the easy one to get wrong.
"""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import PASSWORD


async def _seed_user(engine: AsyncEngine, display_name: str, subject: str) -> None:
    async with engine.begin() as connection:
        user_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, "
                "created_at) VALUES (:id, 'active', :name, false, now())"
            ),
            {"id": user_id, "name": display_name},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), :id, 'local', :subject, "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now()"
            ),
            {"id": user_id, "subject": subject},
        )


def _member(
    client: TestClient, scope: dict[str, str], workspace_id: str, email: str, role: str
) -> dict[str, str]:
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": email, "role": role},
    )
    assert invited.status_code == 201, invited.text
    login = client.post(
        "/api/v1/auth/sessions", json={"subject": email, "password": PASSWORD}
    )
    assert login.status_code == 201, login.text
    return {
        "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
        "X-Workspace-Id": workspace_id,
    }


@pytest.fixture
def secret_ref(client: TestClient, scope: dict[str, str]) -> str:
    created = client.post(
        "/api/v1/secrets",
        headers=scope,
        json={"name": "feishu-encrypt-key", "scope": "workspace", "plaintext": "s" * 32},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["name"])


def _create(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    secret_ref: str,
    **overrides: Any,
) -> Any:
    body: dict[str, Any] = {
        "channel": "feishu",
        "agent_id": agent_id,
        "app_id": "cli_a1b2c3",
        "encrypt_key_ref": secret_ref,
        **overrides,
    }
    return client.post("/api/v1/channel-bindings", headers=headers, json=body)


def test_a_binding_can_be_created_and_then_listed(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    created = _create(client, scope, published_agent, secret_ref)

    assert created.status_code == 201, created.text
    listed = client.get("/api/v1/channel-bindings", headers=scope)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [created.json()["id"]]


def test_no_response_ever_carries_the_key_itself(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """§4.6: `管理元数据，不查看明文`.

    Checked against the whole response body rather than field by field: a
    check that named the fields would keep passing on the day somebody adds
    one, which is exactly when it should fail.
    """
    created = _create(client, scope, published_agent, secret_ref)
    listed = client.get("/api/v1/channel-bindings", headers=scope)

    assert "s" * 32 not in created.text
    assert "s" * 32 not in listed.text
    # The reference is fine — that is what it is for.
    assert secret_ref in created.text


def test_a_feishu_binding_without_a_key_reference_is_refused(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    """Migration 0037's CHECK says a Feishu binding must have one. Refused
    here with something an administrator can act on, rather than reaching
    the database and coming back as an integrity error."""
    refused = _create(client, scope, published_agent, "", encrypt_key_ref=None)

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_required"


def test_a_key_reference_that_names_no_secret_is_refused(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    """A binding pointing at a secret that does not exist accepts deliveries
    it can never decrypt — and fails at the far end, in a webhook, where the
    person who made the mistake is not looking."""
    refused = _create(client, scope, published_agent, "no-such-secret")

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_unknown"


def test_the_same_agent_is_not_bound_to_one_channel_twice(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    first = _create(client, scope, published_agent, secret_ref)
    assert first.status_code == 201, first.text

    again = _create(client, scope, published_agent, secret_ref)

    assert again.status_code == 409, again.text


async def test_a_viewer_cannot_even_see_that_a_channel_exists(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§4.6 gives a viewer `否` on channels — not `只读`.

    The row names an Agent and an app id, which together say this workspace
    publishes that Agent into that Feishu tenant. A viewer is not told.
    """
    assert _create(client, scope, published_agent, secret_ref).status_code == 201
    await _seed_user(engine, "Vera", "vera@example.com")
    viewer = _member(client, scope, workspace_id, "vera@example.com", "viewer")

    refused = client.get("/api/v1/channel-bindings", headers=viewer)

    assert refused.status_code == 403, refused.text


async def test_a_developer_may_see_a_binding_but_not_make_one(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§4.6: a developer `使用已授权绑定`. Using it means knowing it is
    there; creating one is publishing an Agent to the outside world, which
    the same line gives to an administrator."""
    assert _create(client, scope, published_agent, secret_ref).status_code == 201
    # Made before the developer logs in: this `TestClient` has one cookie jar,
    # so `scope`'s CSRF stops matching the session the moment somebody else
    # signs in on it.
    second_agent = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Second", "alias": "second"}
        ).json()["id"]
    )
    await _seed_user(engine, "Dev", "dev@example.com")
    developer = _member(client, scope, workspace_id, "dev@example.com", "developer")

    assert client.get("/api/v1/channel-bindings", headers=developer).status_code == 200

    refused = _create(client, developer, second_agent, secret_ref)

    assert refused.status_code == 403, refused.text


def test_disabling_a_binding_closes_the_door_without_deleting_the_trail(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """Disabled rather than deleted: `channel_events` references the binding,
    and those rows are the record of what this channel already delivered."""
    binding_id = _create(client, scope, published_agent, secret_ref).json()["id"]

    disabled = client.post(
        f"/api/v1/channel-bindings/{binding_id}/disable", headers=scope
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    # Still listed, so somebody can see that this channel used to be open.
    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    assert [item["status"] for item in listed] == ["disabled"]


async def test_a_binding_in_another_workspace_is_invisible(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§23 assertion 1, on the route added last.

    Asked as a **workspace** administrator, not the bootstrap user: that one
    is a platform administrator, and §4.6 lets them act across workspaces so
    long as it is audited. Their `200` with an empty list is correct, and a
    test that used them would have been asserting the wrong subject.
    """
    assert _create(client, scope, published_agent, secret_ref).status_code == 201
    await _seed_user(engine, "Ada", "ada@example.com")
    theirs = _member(client, scope, workspace_id, "ada@example.com", "workspace_admin")
    elsewhere = {**theirs, "X-Workspace-Id": str(uuid4())}

    listed = client.get("/api/v1/channel-bindings", headers=elsewhere)

    assert listed.status_code == 403, listed.text
