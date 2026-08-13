from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def _bearer(token: str, workspace_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id is not None:
        headers["X-Workspace-Id"] = workspace_id
    return headers


def _mint(
    client: TestClient, scope: dict[str, str], scopes: list[str]
) -> tuple[str, str, str]:
    account = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": "runner", "role": "developer"},
    ).json()
    issued = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": scopes},
    ).json()
    return str(account["id"]), str(issued["token"]), str(issued["id"])


async def test_a_write_key_creates_a_run_as_the_service_account(
    client: TestClient,
    scope: dict[str, str],
    published_agent: str,
    engine: AsyncEngine,
) -> None:
    account_id, token, _ = _mint(
        client, scope, ["runs.read", "runs.write", "runs.control"]
    )
    headers = {**_bearer(token), "Idempotency-Key": "machine-1"}
    session = client.post(
        "/api/v1/sessions",
        headers=headers,
        json={"agent_id": published_agent},
    )
    assert session.status_code == 201
    assert session.json()["caller_type"] == "service_account"
    assert session.json()["caller_id"] == account_id

    created = client.post(
        "/api/v1/runs",
        headers=headers,
        json={"session_id": session.json()["id"], "input": "hello from a key"},
    )
    assert created.status_code == 201
    assert created.json()["session_id"] == session.json()["id"]

    async with engine.connect() as connection:
        caller = (
            await connection.execute(
                text("SELECT caller_type, caller_id FROM sessions WHERE id = :id"),
                {"id": session.json()["id"]},
            )
        ).one()
        audit_type = (
            await connection.execute(
                text(
                    "SELECT actor_type FROM audit_events "
                    "WHERE action = 'run.created' ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
    assert caller[0] == "service_account"
    assert str(caller[1]) == account_id
    assert audit_type == "service_account"


async def test_idempotency_is_scoped_to_the_account_not_the_key(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    account = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": "shared", "role": "developer"},
    ).json()
    first = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.read", "runs.write"]},
    ).json()
    second = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.read", "runs.write"]},
    ).json()
    session = client.post(
        "/api/v1/sessions",
        headers={**_bearer(first["token"]), "Idempotency-Key": "session"},
        json={"agent_id": published_agent},
    ).json()
    body = {"session_id": session["id"], "input": "once"}
    created = client.post(
        "/api/v1/runs",
        headers={**_bearer(first["token"]), "Idempotency-Key": "same"},
        json=body,
    )
    replayed = client.post(
        "/api/v1/runs",
        headers={**_bearer(second["token"]), "Idempotency-Key": "same"},
        json=body,
    )
    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert replayed.json()["id"] == created.json()["id"]

    other = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": "other", "role": "developer"},
    ).json()
    other_key = client.post(
        f"/api/v1/service-accounts/{other['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.read", "runs.write"]},
    ).json()
    other_session = client.post(
        "/api/v1/sessions",
        headers={**_bearer(other_key["token"]), "Idempotency-Key": "session"},
        json={"agent_id": published_agent},
    ).json()
    other_run = client.post(
        "/api/v1/runs",
        headers={**_bearer(other_key["token"]), "Idempotency-Key": "same"},
        json={"session_id": other_session["id"], "input": "once"},
    )
    assert other_run.status_code == 201
    assert other_run.json()["id"] != created.json()["id"]


async def test_read_scope_cannot_create_and_write_cannot_pause(
    client: TestClient, scope: dict[str, str], published_agent: str, session_id: str
) -> None:
    _, reader, _ = _mint(client, scope, ["runs.read"])
    refused = client.post(
        "/api/v1/runs",
        headers={**_bearer(reader), "Idempotency-Key": "nope"},
        json={"session_id": session_id, "input": "no"},
    )
    assert refused.status_code == 403

    account = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": "writer-only", "role": "developer"},
    ).json()
    writer_key = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.read", "runs.write"]},
    ).json()["token"]
    session = client.post(
        "/api/v1/sessions",
        headers={**_bearer(writer_key), "Idempotency-Key": "s"},
        json={"agent_id": published_agent},
    ).json()
    run = client.post(
        "/api/v1/runs",
        headers={**_bearer(writer_key), "Idempotency-Key": "r"},
        json={"session_id": session["id"], "input": "pause me"},
    ).json()
    paused = client.post(
        f"/api/v1/runs/{run['id']}/pause",
        headers=_bearer(writer_key),
        json={"expected_state_version": run["state_version"]},
    )
    assert paused.status_code == 403


async def test_a_key_cannot_switch_workspace_via_header(
    client: TestClient, scope: dict[str, str], admin_csrf: str, published_agent: str
) -> None:
    _, token, _ = _mint(client, scope, ["runs.read", "runs.write"])
    other = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Elsewhere"},
    ).json()
    crossed = client.post(
        "/api/v1/sessions",
        headers={**_bearer(token, other["id"]), "Idempotency-Key": "x"},
        json={"agent_id": published_agent},
    )
    assert crossed.status_code == 403
    assert crossed.json()["code"] == "forbidden"


async def test_cookie_writes_still_require_csrf(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    missing = client.post(
        "/api/v1/sessions",
        headers={"X-Workspace-Id": scope["X-Workspace-Id"]},
        json={"agent_id": published_agent},
    )
    assert missing.status_code == 403
    assert missing.json()["code"] == "csrf_failed"
