"""§21's last wizard step, against the real catalogue.

The unit tests say the shipped spec is a valid `AgentSpec`. That is not the
question an administrator has at the end of the setup wizard. Theirs is
whether the button produces something they can run — and between a valid
spec and a runnable Agent sit every publish-time rule: the endpoint, the
network allow-list, the ceilings, the tool names. An example that validates
and then cannot be published is the worse failure, because it fails later
and on their deployment rather than in our test suite.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.agents.domain.examples import EXAMPLES

from ..conftest import PASSWORD

CREDENTIAL = "TINY_HERMES_TEST_MODEL_KEY"

ENDPOINT: dict[str, Any] = {
    "name": "acme-gpt",
    "kind": "openai_compatible",
    "base_url": "https://models.example.com/v1",
    "model": "acme-large",
    "context_window": 128_000,
    "max_output_tokens": 4_096,
    "usage_quality": "provider",
    "credential_ref": CREDENTIAL,
}


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")


@pytest.fixture
def endpoint_id(client: TestClient, admin_csrf: str) -> str:
    created = client.post(
        "/api/v1/model-endpoints", headers={"X-CSRF-Token": admin_csrf}, json=ENDPOINT
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def test_every_shipped_example_can_actually_be_created_and_published(
    client: TestClient, scope: dict[str, str], endpoint_id: str
) -> None:
    for example in EXAMPLES:
        created = client.post(
            f"/api/v1/agents/examples/{example.slug}",
            headers=scope,
            json={"endpoint_id": endpoint_id},
        )

        assert created.status_code == 201, created.text
        body = created.json()
        # A published version, not a draft. A draft is not runnable, and the
        # wizard step is "create an example Agent", not "start one".
        assert body["version_id"] is not None
        agent_id = body["agent"]["id"]
        versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=scope)
        assert versions.status_code == 200, versions.text
        assert len(versions.json()) == 1


def test_the_example_is_listed_before_it_is_created(
    client: TestClient, scope: dict[str, str]
) -> None:
    # The console cannot offer a button for something it has to know the name
    # of in advance; a hardcoded slug on the web side is a second place for
    # the catalogue to live.
    listed = client.get("/api/v1/agents/examples", headers=scope)

    assert listed.status_code == 200, listed.text
    assert {item["slug"] for item in listed.json()} == {
        example.slug for example in EXAMPLES
    }


def test_an_example_this_deployment_does_not_ship_is_a_404(
    client: TestClient, scope: dict[str, str], endpoint_id: str
) -> None:
    refused = client.post(
        "/api/v1/agents/examples/no-such-example",
        headers=scope,
        json={"endpoint_id": endpoint_id},
    )

    assert refused.status_code == 404, refused.text
    assert refused.json()["code"] == "agent_example_not_found"


def test_creating_the_example_twice_is_refused_rather_than_silently_duplicated(
    client: TestClient, scope: dict[str, str], endpoint_id: str
) -> None:
    """The alias is fixed, and aliases are unique.

    Worth pinning because the failure mode people expect is the other one: a
    second Agent named the same thing, and a workspace where nobody can tell
    which of the two the wizard made.
    """
    first = client.post(
        "/api/v1/agents/examples/notes-tidier",
        headers=scope,
        json={"endpoint_id": endpoint_id},
    )
    assert first.status_code == 201, first.text

    again = client.post(
        "/api/v1/agents/examples/notes-tidier",
        headers=scope,
        json={"endpoint_id": endpoint_id},
    )

    assert again.status_code == 409, again.text
    assert again.json()["code"] == "agent_alias_taken"


async def _seed_user(engine: AsyncEngine, display_name: str, subject: str) -> UUID:
    user_id = uuid4()
    async with engine.begin() as connection:
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
    return user_id


async def test_a_viewer_cannot_create_the_example(
    client: TestClient,
    scope: dict[str, str],
    endpoint_id: str,
    workspace_id: str,
    engine: AsyncEngine,
) -> None:
    """§4.6 gives a viewer no write on agents, and this is a write — a
    convenience route that skipped the role check would be a way to publish
    an Agent without the permission publishing needs."""
    await _seed_user(engine, "Vera", "vera@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "vera@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "vera@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201, login.text
    viewer = {
        "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
        "X-Workspace-Id": workspace_id,
    }

    refused = client.post(
        "/api/v1/agents/examples/notes-tidier",
        headers=viewer,
        json={"endpoint_id": endpoint_id},
    )

    assert refused.status_code == 403, refused.text
