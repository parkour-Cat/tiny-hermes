"""One end-to-end tracer through the public phase-2A HTTP surface.

Only the future Worker's terminal signal uses the application seam; every other
step is a real authenticated request, and no database row is edited directly.
"""

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.domain.models import RunCapabilities, RunSignal
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import ApplySignalCommand

from ..conftest import BOOTSTRAP_TOKEN, PASSWORD, VALID_SPEC

FULL = RunCapabilities(can_control=True, can_retry=True)

AUDIT_ACTIONS = (
    "SELECT action, count(*) FROM audit_events GROUP BY action ORDER BY action"
)


async def _worker_signal(
    engine: AsyncEngine, workspace_id: str, run_id: str, signal: RunSignal
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        await RunCoordination(SqlRunStore(session)).apply_signal(
            ApplySignalCommand(
                workspace_id=UUID(workspace_id),
                run_id=UUID(run_id),
                signal=signal,
                request_id=f"worker-{signal.value}",
                capabilities=FULL,
            )
        )


async def test_the_whole_phase_two_a_surface_works_over_http(
    client: TestClient, engine: AsyncEngine
) -> None:
    assert (
        client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={
                "subject": "operator@example.com",
                "display_name": "Operator",
                "password": PASSWORD,
            },
        ).status_code
        == 201
    )
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "operator@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201
    write = {"X-CSRF-Token": login.cookies["tiny_hermes_csrf"]}

    workspace = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Tracer"}
    ).json()
    other = client.post(
        "/api/v1/workspaces", headers=write, json={"name": "Elsewhere"}
    ).json()
    scope = {"X-Workspace-Id": workspace["id"], **write}
    crossed = {"X-Workspace-Id": other["id"], **write}

    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Tracer", "alias": "tracer"}
    ).json()
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    ).json()
    version = client.post(
        f"/api/v1/agents/{agent['id']}/publish",
        headers=scope,
        json={"expected_revision": draft["revision"]},
    ).json()

    session = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": agent["id"]}
    ).json()
    assert client.get("/api/v1/sessions", headers=scope).json()[0]["id"] == session["id"]

    first = _create(client, scope, session["id"], "key-1")
    second = _create(client, scope, session["id"], "key-2")
    third = _create(client, scope, session["id"], "key-3")
    assert {run["agent_version_id"] for run in (first, second, third)} == {version["id"]}

    replay = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "key-1"},
        json={"session_id": session["id"], "input": "message key-1"},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == first["id"]

    paused = client.post(
        f"/api/v1/runs/{second['id']}/pause",
        headers=scope,
        json={"expected_state_version": 1},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    resumed = client.post(
        f"/api/v1/runs/{second['id']}/resume",
        headers=scope,
        json={"expected_state_version": paused.json()["state_version"]},
    )
    assert resumed.status_code == 200
    cancelled = client.post(
        f"/api/v1/runs/{second['id']}/cancel",
        headers=scope,
        json={"expected_state_version": resumed.json()["state_version"]},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    await _worker_signal(engine, workspace["id"], str(first["id"]), RunSignal.LEASE_ACQUIRED)
    await _worker_signal(engine, workspace["id"], str(first["id"]), RunSignal.COMPLETED)

    reloaded_session = client.get(
        f"/api/v1/sessions/{session['id']}", headers=scope
    ).json()
    assert reloaded_session["head_run_id"] == third["id"]

    listed = client.get(f"/api/v1/runs?session_id={session['id']}", headers=scope).json()
    assert [item["session_sequence"] for item in listed] == [1, 2, 3]
    assert [item["status"] for item in listed] == ["completed", "cancelled", "queued"]
    assert [item["queue"]["status"] for item in listed] == [
        "terminal",
        "terminal",
        "head",
    ]
    assert listed[2]["blocked_by_run_id"] is None
    assert listed[2]["available_actions"] == ["pause", "cancel"]
    for item in listed:
        assert item["budget_root_run_id"] == item["id"]
        assert item["retry_of_run_id"] is None
        assert item["state_version"] >= 1
        assert item["budget"]["max_derived_retries"] == 3

    unsafe = client.post(
        f"/api/v1/runs/{listed[0]['id']}/retry",
        headers={**scope, "Idempotency-Key": "retry-1"},
        json={},
    )
    assert unsafe.status_code == 409
    assert unsafe.json()["code"] == "retry_not_safe"

    for path in (
        f"/api/v1/agents/{agent['id']}",
        f"/api/v1/sessions/{session['id']}",
        f"/api/v1/runs/{third['id']}",
    ):
        response = client.get(path, headers=crossed)
        assert response.status_code == 404
        assert "Tracer" not in str(response.json())
        assert "concise" not in str(response.json())

    async with engine.connect() as connection:
        audits = dict(
            (row[0], int(row[1]))
            for row in (await connection.execute(text(AUDIT_ACTIONS))).all()
        )
    assert audits["workspace.created"] == 2
    assert audits["agent.created"] == 1
    assert audits["agent.draft_replaced"] == 1
    assert audits["agent.published"] == 1
    assert audits["session.created"] == 1
    assert audits["run.created"] == 3
    assert audits["run.pause_requested"] == 1
    assert audits["run.resume_requested"] == 1
    assert audits["run.cancel_requested"] == 1
    assert audits["run.lease_acquired"] == 1
    assert audits["run.completed"] == 1


def _create(
    client: TestClient, scope: dict[str, str], session_id: str, key: str
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201
    return dict(response.json())
