from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings

TRUNCATE = (
    "TRUNCATE idempotency_records, worker_leases, run_events, run_budget_scopes, "
    "session_messages, runs, sessions, agent_versions, agent_drafts, agents, "
    "audit_events, memberships, workspaces, auth_sessions, auth_identities, users CASCADE"
)

SPEC: dict[str, Any] = {
    "schema_version": 1,
    "personality": "You are concise.",
    "model_policy": {"provider": "deterministic", "scenario": "complete"},
    "tools": [],
    "limits": {
        "max_execution_seconds": 900,
        "max_elapsed_seconds": 86400,
        "max_model_calls": 20,
        "max_tool_calls": 50,
        "max_derived_retries": 3,
    },
}


def build_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/0",
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token="a" * 32,
    )


def bootstrap_admin(client: TestClient, subject: str = "admin@example.com") -> str:
    assert (
        client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": "a" * 32},
            json={
                "subject": subject,
                "display_name": "Admin",
                "password": "long-pass-123",
            },
        ).status_code
        == 201
    )
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": subject, "password": "long-pass-123"},
    )
    assert login.status_code == 201
    return login.cookies["tiny_hermes_csrf"]


@pytest.fixture
async def client(engine: AsyncEngine, database_url: str) -> AsyncIterator[TestClient]:
    async with engine.begin() as connection:
        await connection.execute(text(TRUNCATE))
    with TestClient(create_app(settings=build_settings(database_url))) as value:
        yield value


async def test_agent_publication_workflow_keeps_versions_immutable(
    client: TestClient,
) -> None:
    csrf = bootstrap_admin(client)
    write = {"X-CSRF-Token": csrf}
    workspace = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Primary"}
    ).json()
    scope = {"X-Workspace-Id": workspace["id"], **write}

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
        json={"expected_revision": 1, "spec": SPEC},
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

    changed: dict[str, Any] = {**SPEC, "personality": "You are thorough."}
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
    client: TestClient,
) -> None:
    csrf = bootstrap_admin(client)
    write = {"X-CSRF-Token": csrf}
    workspace = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Primary"}
    ).json()
    scope = {"X-Workspace-Id": workspace["id"], **write}
    agent_id = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
    ).json()["id"]

    client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": SPEC},
    )
    stale = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": SPEC},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "draft_revision_conflict"

    current = client.get(f"/api/v1/agents/{agent_id}/draft", headers=scope)
    assert current.json()["revision"] == 2


async def test_cross_workspace_agent_identifier_is_a_generic_not_found(
    client: TestClient,
) -> None:
    csrf = bootstrap_admin(client)
    write = {"X-CSRF-Token": csrf}
    first_workspace = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Primary"}
    ).json()
    second_workspace = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Secondary"}
    ).json()

    agent_id = client.post(
        "/api/v1/agents",
        headers={"X-Workspace-Id": first_workspace["id"], **write},
        json={"name": "Analyst", "alias": "analyst"},
    ).json()["id"]

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
    client: TestClient, engine: AsyncEngine
) -> None:
    csrf = bootstrap_admin(client)
    write = {"X-CSRF-Token": csrf}
    workspace = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Primary"}
    ).json()
    agent_id = client.post(
        "/api/v1/agents",
        headers={"X-Workspace-Id": workspace["id"], **write},
        json={"name": "Analyst", "alias": "analyst"},
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
        json={"subject": "outsider@example.com", "password": "long-pass-123"},
    )
    assert login.status_code == 201
    assert login.json()["is_platform_admin"] is False

    denied = client.get(
        f"/api/v1/agents/{agent_id}", headers={"X-Workspace-Id": workspace["id"]}
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"
