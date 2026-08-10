import asyncio

import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

COUNTS = (
    "SELECT "
    "(SELECT count(*) FROM session_messages) AS messages, "
    "(SELECT count(*) FROM runs) AS runs, "
    "(SELECT count(*) FROM run_budget_scopes) AS budgets, "
    "(SELECT count(*) FROM run_events) AS events, "
    "(SELECT count(*) FROM idempotency_records) AS records, "
    "(SELECT count(*) FROM audit_events WHERE action = 'run.created' "
    " AND result = 'succeeded') AS audits"
)


async def _counts(engine: AsyncEngine) -> tuple[int, ...]:
    async with engine.connect() as connection:
        row = (await connection.execute(text(COUNTS))).one()
    return tuple(int(value) for value in row)


def _submit(
    client: TestClient, scope: dict[str, str], session_id: str, key: str, text_input: str
) -> httpx.Response:
    return client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": text_input},
    )


async def test_three_runs_form_a_fifo_queue_behind_one_head(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    runs = [
        _submit(client, scope, session_id, f"key-{index}", f"message {index}")
        for index in range(1, 4)
    ]
    assert [run.status_code for run in runs] == [201, 201, 201]
    bodies = [run.json() for run in runs]

    session = client.get(f"/api/v1/sessions/{session_id}", headers=scope).json()
    assert session["next_run_sequence"] == 4
    assert session["next_message_sequence"] == 4
    assert session["head_run_id"] == bodies[0]["id"]

    assert [body["session_sequence"] for body in bodies] == [1, 2, 3]
    assert bodies[0]["blocked_by_run_id"] is None
    assert bodies[0]["queue"] == {"position": 1, "status": "head"}
    assert bodies[1]["blocked_by_run_id"] == bodies[0]["id"]
    assert bodies[2]["blocked_by_run_id"] == bodies[0]["id"]
    assert [body["queue"]["position"] for body in bodies] == [1, 2, 3]
    assert {body["queue"]["status"] for body in bodies[1:]} == {"pending"}
    assert {body["status"] for body in bodies} == {"queued"}

    version_ids = {body["agent_version_id"] for body in bodies}
    assert len(version_ids) == 1

    for body in bodies:
        assert body["budget_root_run_id"] == body["id"]
        assert body["retry_of_run_id"] is None
        assert body["last_event_sequence"] == 1
        assert body["state_version"] == 1

    async with engine.connect() as connection:
        budgets = (
            await connection.execute(text("SELECT count(*) FROM run_budget_scopes"))
        ).scalar_one()
        events = (
            await connection.execute(
                text(
                    "SELECT run_id, count(*), min(sequence), min(event_type) "
                    "FROM run_events GROUP BY run_id"
                )
            )
        ).all()
        sequences = (
            await connection.execute(text("SELECT next_event_sequence FROM runs"))
        ).scalars().all()

    assert budgets == 3
    assert len(events) == 3
    assert {(int(row[1]), int(row[2]), row[3]) for row in events} == {(1, 1, "run_created")}
    assert set(sequences) == {2}


async def test_run_creation_requires_an_idempotency_key(
    client: TestClient, scope: dict[str, str], session_id: str
) -> None:
    missing = client.post(
        "/api/v1/runs", headers=scope, json={"session_id": session_id, "input": "hello"}
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "idempotency_key_required"

    blank = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "   "},
        json={"session_id": session_id, "input": "hello"},
    )
    assert blank.status_code == 400
    assert blank.json()["code"] == "idempotency_key_required"


async def test_concurrent_equal_keys_create_exactly_one_run(
    concurrent_client: httpx.AsyncClient,
    scope: dict[str, str],
    session_id: str,
    engine: AsyncEngine,
) -> None:
    before = await _counts(engine)
    body = {"session_id": session_id, "input": "only once"}
    headers = {**scope, "Idempotency-Key": "shared-key"}

    first, second = await asyncio.gather(
        concurrent_client.post("/api/v1/runs", headers=headers, json=body),
        concurrent_client.post("/api/v1/runs", headers=headers, json=body),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 201]
    assert first.json()["id"] == second.json()["id"]

    replayed = first if first.status_code == 200 else second
    created = second if first.status_code == 200 else first
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in created.headers
    assert created.headers["Location"].endswith(f"/api/v1/runs/{created.json()['id']}")

    after = await _counts(engine)
    assert [value - base for base, value in zip(before, after, strict=True)] == [1, 1, 1, 1, 1, 1]


async def test_the_same_key_with_a_different_request_conflicts(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    assert _submit(client, scope, session_id, "reused", "first").status_code == 201
    before = await _counts(engine)

    conflict = _submit(client, scope, session_id, "reused", "second")

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_reused"
    assert await _counts(engine) == before


async def test_replaying_a_key_returns_the_original_snapshot(
    client: TestClient, scope: dict[str, str], session_id: str
) -> None:
    created = _submit(client, scope, session_id, "replay", "hello")
    replay = _submit(client, scope, session_id, "replay", "hello")

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == created.json()


async def test_runs_can_only_be_created_for_a_published_agent(
    client: TestClient, scope: dict[str, str]
) -> None:
    agent_id = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Unpublished", "alias": "draft-only"}
    ).json()["id"]
    session = client.post("/api/v1/sessions", headers=scope, json={"agent_id": agent_id})
    assert session.status_code == 201

    denied = _submit(client, scope, str(session.json()["id"]), "key-1", "hello")
    assert denied.status_code == 409
    assert denied.json()["code"] == "agent_not_published"


async def test_cross_workspace_session_identifier_is_a_generic_not_found(
    client: TestClient, scope: dict[str, str], session_id: str, admin_csrf: str
) -> None:
    other = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Secondary"},
    ).json()
    crossed = client.post(
        "/api/v1/runs",
        headers={
            "X-Workspace-Id": other["id"],
            "X-CSRF-Token": admin_csrf,
            "Idempotency-Key": "key-1",
        },
        json={"session_id": session_id, "input": "hello"},
    )
    assert crossed.status_code == 404
    assert crossed.json()["code"] == "session_not_found"


async def test_run_list_and_detail_report_queue_order(
    client: TestClient, scope: dict[str, str], session_id: str
) -> None:
    first = _submit(client, scope, session_id, "key-1", "one").json()
    second = _submit(client, scope, session_id, "key-2", "two").json()

    listed = client.get(f"/api/v1/runs?session_id={session_id}", headers=scope)
    assert [item["id"] for item in listed.json()] == [first["id"], second["id"]]
    assert [item["queue"]["position"] for item in listed.json()] == [1, 2]

    detail = client.get(f"/api/v1/runs/{second['id']}", headers=scope)
    assert detail.status_code == 200
    assert detail.json()["queue"] == {"position": 2, "status": "pending"}
    assert detail.json()["available_actions"] == ["pause", "cancel"]
