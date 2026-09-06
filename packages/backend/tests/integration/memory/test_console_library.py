from uuid import UUID, uuid4
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def create(client: TestClient, scope: dict[str, str], agent: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/memories/shared",
        headers=scope,
        json={"agent_id": agent, "body": "Original shared memory"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_shared_memory_can_be_found_corrected_and_retired(
    client: TestClient,
    scope: dict[str, str],
    published_agent: str,
) -> None:
    row = create(client, scope, published_agent)
    listed = client.get(
        "/api/v1/memories",
        headers=scope,
        params={"kind": "shared", "agent_id": published_agent, "status": "active"},
    )
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == row["id"] for item in listed.json()["items"])
    edited = client.patch(
        f"/api/v1/memories/shared/{row['id']}",
        headers=scope,
        json={"body": "Corrected memory", "expected_updated_at": row["updated_at"]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"] == "Corrected memory"
    stale = client.patch(
        f"/api/v1/memories/shared/{row['id']}",
        headers=scope,
        json={"body": "Stale edit", "expected_updated_at": row["updated_at"]},
    )
    assert stale.status_code == 409, stale.text
    retired = client.patch(
        f"/api/v1/memories/shared/{row['id']}",
        headers=scope,
        json={"status": "rejected", "expected_updated_at": edited.json()["updated_at"]},
    )
    assert retired.status_code == 200, retired.text
    history = client.get("/api/v1/memories", headers=scope, params={"status": "rejected"})
    assert (
        next(item for item in history.json()["items"] if item["id"] == row["id"])["body"]
        == "Corrected memory"
    )


def test_memory_list_reports_more_and_respects_agent_filter(
    client: TestClient,
    scope: dict[str, str],
    published_agent: str,
) -> None:
    rows = [create(client, scope, published_agent) for _ in range(2)]
    first = client.get("/api/v1/memories", headers=scope, params={"limit": 1})
    assert first.status_code == 200, first.text
    assert first.json()["has_more"] is True
    second = client.get("/api/v1/memories", headers=scope, params={"limit": 1, "offset": 1})
    assert {first.json()["items"][0]["id"], second.json()["items"][0]["id"]} == {
        r["id"] for r in rows
    }
    absent = client.get("/api/v1/memories", headers=scope, params={"agent_id": str(uuid4())})
    assert absent.json()["items"] == []


async def test_viewer_cannot_read_or_modify_shared_library(
    client: TestClient,
    scope: dict[str, str],
    published_agent: str,
    engine: AsyncEngine,
) -> None:
    row = create(client, scope, published_agent)
    async with engine.begin() as db:
        await db.execute(text("UPDATE users SET is_platform_admin=false"))
        await db.execute(
            text("UPDATE memberships SET role='viewer' WHERE workspace_id=:w"),
            {"w": UUID(scope["X-Workspace-Id"])},
        )
    assert client.get("/api/v1/memories", headers=scope).status_code == 403
    assert (
        client.patch(
            f"/api/v1/memories/shared/{row['id']}",
            headers=scope,
            json={"body": "Unauthorized", "expected_updated_at": row["updated_at"]},
        ).status_code
        == 403
    )
