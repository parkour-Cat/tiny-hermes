from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import PASSWORD, VALID_SPEC


async def test_agent_publication_workflow_keeps_versions_immutable(
    client: TestClient, scope: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["current_version_id"] is None

    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200
    assert draft.json()["revision"] == 2

    first = client.post(
        f"/api/v1/agents/{agent_id}/publish", headers=scope, json={"expected_revision": 2}
    )
    assert first.status_code == 201
    assert first.json()["version_number"] == 1
    assert len(first.json()["content_hash"]) == 64
    assert "personality" not in str(first.json())

    unchanged = client.post(
        f"/api/v1/agents/{agent_id}/publish", headers=scope, json={"expected_revision": 2}
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["id"] == first.json()["id"]

    changed: dict[str, Any] = {**VALID_SPEC, "personality": "You are thorough."}
    second_draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 2, "spec": changed},
    )
    assert second_draft.status_code == 200
    second = client.post(
        f"/api/v1/agents/{agent_id}/publish", headers=scope, json={"expected_revision": 3}
    )
    assert second.status_code == 201
    assert second.json()["version_number"] == 2

    rolled_back = client.post(
        f"/api/v1/agents/{agent_id}/rollback",
        headers=scope,
        json={"version_id": first.json()["id"]},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["id"] == first.json()["id"]

    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=scope)
    assert [item["version_number"] for item in versions.json()] == [1, 2]
    reloaded = client.get(f"/api/v1/agents/{agent_id}", headers=scope)
    assert reloaded.json()["current_version_id"] == first.json()["id"]
    assert reloaded.json()["status"] == "published"


async def test_stale_draft_revision_conflicts_without_overwriting(
    client: TestClient, scope: dict[str, str]
) -> None:
    agent_id = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
    ).json()["id"]

    client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    stale = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "draft_revision_conflict"

    current = client.get(f"/api/v1/agents/{agent_id}/draft", headers=scope)
    assert current.json()["revision"] == 2


async def test_a_taken_alias_is_refused_by_the_database_adapter_too(
    client: TestClient, scope: dict[str, str]
) -> None:
    """The alias rule has to survive the adapter swap.

    The in-memory adapter checks for a duplicate before inserting, so its unit
    test passes while PostgreSQL raises ``uq_agents_workspace_alias`` instead.
    Only an integration test can tell the two apart, and without one the 409
    branch is dead code and callers get a 500 for a typo.
    """
    first = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Second", "alias": "analyst"}
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "agent_alias_taken"
    assert len(client.get("/api/v1/agents", headers=scope).json()) == 1


async def test_the_same_alias_is_free_in_another_workspace(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    """The uniqueness is per Workspace, so the translation must not overreach."""
    client.post("/api/v1/agents", headers=scope, json={"name": "A", "alias": "analyst"})
    second_workspace = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Secondary"},
    ).json()

    reused = client.post(
        "/api/v1/agents",
        headers={**scope, "X-Workspace-Id": second_workspace["id"]},
        json={"name": "B", "alias": "analyst"},
    )

    assert reused.status_code == 201


async def test_cross_workspace_agent_identifier_is_a_generic_not_found(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    agent_id = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
    ).json()["id"]
    second_workspace = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Secondary"},
    ).json()

    crossed = client.get(
        f"/api/v1/agents/{agent_id}",
        headers={"X-Workspace-Id": second_workspace["id"]},
    )
    assert crossed.status_code == 404
    assert crossed.json()["code"] == "agent_not_found"
    body = str(crossed.json())
    assert "Analyst" not in body
    assert "analyst" not in body
    assert "concise" not in body


async def test_missing_membership_is_forbidden_before_resource_lookup(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    agent_id = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
    ).json()["id"]

    # No self-service registration exists yet, so the second local account is
    # seeded directly with the same verifier as the bootstrap administrator.
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

    denied = client.get(
        f"/api/v1/agents/{agent_id}",
        headers={"X-Workspace-Id": scope["X-Workspace-Id"]},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"
