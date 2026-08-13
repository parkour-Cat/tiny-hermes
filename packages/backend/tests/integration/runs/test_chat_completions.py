"""Inbound Chat Completions is a thin adapter over Run Coordination.

Cookie sessions are refused here so a browser cannot skip CSRF. A persistent
Session that is already blocked returns 409 and inserts no Run.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import Any, cast

import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier

COMPLETIONS = "/v1/chat/completions"
FINISHED = "The deterministic scenario finished."
RUN_COUNT = text("SELECT count(*) FROM runs")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(client: TestClient, scope: dict[str, str], name: str = "completer") -> str:
    account = client.post(
        "/api/v1/service-accounts",
        headers=scope,
        json={"name": name, "role": "developer"},
    ).json()
    issued = client.post(
        f"/api/v1/service-accounts/{account['id']}/api-keys",
        headers=scope,
        json={"scopes": ["runs.read", "runs.write", "runs.control"]},
    ).json()
    return str(issued["token"])


def _enabled_spec() -> dict[str, Any]:
    return {
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
        "delivery": {"enabled": True, "sync_timeout_seconds": 15},
    }


def _publish_enabled(client: TestClient, scope: dict[str, str], alias: str) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents",
            headers=scope,
            json={"name": alias.title(), "alias": alias},
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": _enabled_spec()},
    )
    assert draft.status_code == 200
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201
    return agent_id


def _body(model: str, content: str = "hello") -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": content}]}


async def _count_runs(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(RUN_COUNT)).scalar_one())


@contextlib.asynccontextmanager
async def _worker_pump(engine: AsyncEngine) -> AsyncGenerator[None]:
    """Claim work on this event loop so Completions can wait without blocking it."""
    stop = asyncio.Event()
    worker = WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="completions-worker",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    )

    async def pump() -> None:
        while not stop.is_set():
            try:
                advanced = await worker.run_once()
            except Exception:
                advanced = None
            if advanced is None and not stop.is_set():
                await asyncio.sleep(0.02)

    task = asyncio.create_task(pump())
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


def test_a_cookie_post_is_not_a_csrf_bypass(
    client: TestClient, scope: dict[str, str]
) -> None:
    refused = client.post(COMPLETIONS, headers=scope, json=_body("analyst"))
    assert refused.status_code in {401, 403}


def test_an_unknown_alias_is_model_not_found(
    client: TestClient, scope: dict[str, str]
) -> None:
    token = _mint(client, scope)
    refused = client.post(
        COMPLETIONS,
        headers={**_bearer(token), "Idempotency-Key": "missing"},
        json=_body("no-such-agent"),
    )
    assert refused.status_code == 404
    body = cast(dict[str, Any], refused.json())
    assert body["error"]["code"] == "model_not_found"


def test_a_disabled_agent_is_not_compatible(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    del published_agent
    token = _mint(client, scope)
    refused = client.post(
        COMPLETIONS,
        headers={**_bearer(token), "Idempotency-Key": "off"},
        json=_body("analyst"),
    )
    assert refused.status_code == 400
    body = cast(dict[str, Any], refused.json())
    assert body["error"]["code"] == "agent_not_compatible"


async def test_a_blocked_persistent_session_creates_zero_runs(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
) -> None:
    token = _mint(client, scope)
    agent_id = _publish_enabled(client, scope, "blocker")
    session = client.post(
        "/api/v1/sessions",
        headers={**_bearer(token), "Idempotency-Key": "session"},
        json={"agent_id": agent_id},
    ).json()
    created = client.post(
        "/api/v1/runs",
        headers={**_bearer(token), "Idempotency-Key": "head"},
        json={"session_id": session["id"], "input": "pause me"},
    ).json()
    paused = client.post(
        f"/api/v1/runs/{created['id']}/pause",
        headers=_bearer(token),
        json={"expected_state_version": created["state_version"]},
    )
    assert paused.status_code == 200
    before = await _count_runs(engine)

    refused = client.post(
        COMPLETIONS,
        headers={
            **_bearer(token),
            "Idempotency-Key": "blocked",
            "X-Tiny-Hermes-Session-Id": str(session["id"]),
        },
        json=_body("blocker"),
    )
    assert refused.status_code == 409
    error = cast(dict[str, Any], refused.json())["error"]
    assert error["code"] == "session_blocked"
    assert error["session_id"] == session["id"]
    assert error["blocked_by_run_id"] == created["id"]
    assert error["head_status"] == "paused"
    assert error["head_reason"] == {
        "pause_reason": "manual",
        "wait_kind": None,
        "wait_deadline_at": None,
    }
    assert set(error["available_actions"]) == {"resume", "cancel"}
    assert "runs_api_url" in error
    assert await _count_runs(engine) == before


async def test_a_foreign_session_header_does_not_fall_back(
    client: TestClient,
    scope: dict[str, str],
    published_agent: str,
    engine: AsyncEngine,
) -> None:
    token = _mint(client, scope)
    _publish_enabled(client, scope, "owned")
    foreign = client.post(
        "/api/v1/sessions",
        headers=scope,
        json={"agent_id": published_agent},
    ).json()
    before = await _count_runs(engine)

    refused = client.post(
        COMPLETIONS,
        headers={
            **_bearer(token),
            "Idempotency-Key": "foreign",
            "X-Tiny-Hermes-Session-Id": str(foreign["id"]),
        },
        json=_body("owned"),
    )
    assert refused.status_code == 404
    assert cast(dict[str, Any], refused.json())["error"]["code"] == "session_not_found"
    assert await _count_runs(engine) == before


async def test_default_completions_creates_an_ephemeral_session(
    concurrent_client: httpx.AsyncClient,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
) -> None:
    token = _mint(client, scope)
    _publish_enabled(client, scope, "completer")
    async with _worker_pump(engine):
        response = await concurrent_client.post(
            COMPLETIONS,
            headers={**_bearer(token), "Idempotency-Key": "cc-1"},
            json=_body("completer"),
        )
    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == FINISHED
    async with engine.connect() as connection:
        modes = (
            await connection.execute(text("SELECT session_mode FROM sessions"))
        ).scalars().all()
        statuses = (await connection.execute(text("SELECT status FROM runs"))).scalars().all()
    assert list(modes) == ["ephemeral"]
    assert list(statuses) == ["completed"]


async def test_a_persistent_header_appends_to_the_same_session(
    concurrent_client: httpx.AsyncClient,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
) -> None:
    token = _mint(client, scope)
    agent_id = _publish_enabled(client, scope, "kept")
    session = client.post(
        "/api/v1/sessions",
        headers={**_bearer(token), "Idempotency-Key": "keep"},
        json={"agent_id": agent_id},
    ).json()
    session_header = {"X-Tiny-Hermes-Session-Id": str(session["id"])}
    async with _worker_pump(engine):
        first = await concurrent_client.post(
            COMPLETIONS,
            headers={**_bearer(token), "Idempotency-Key": "keep-1", **session_header},
            json=_body("kept"),
        )
        second = await concurrent_client.post(
            COMPLETIONS,
            headers={**_bearer(token), "Idempotency-Key": "keep-2", **session_header},
            json=_body("kept", "again"),
        )
    assert first.status_code == 200
    assert second.status_code == 200
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT session_id, session_sequence, status FROM runs "
                    "ORDER BY session_sequence"
                )
            )
        ).all()
    assert len(rows) == 2
    assert {str(row[0]) for row in rows} == {str(session["id"])}
    assert [row[1] for row in rows] == [1, 2]
    assert {row[2] for row in rows} == {"completed"}


async def test_idempotency_replays_and_rejects_a_different_body(
    concurrent_client: httpx.AsyncClient,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
) -> None:
    token = _mint(client, scope)
    _publish_enabled(client, scope, "once")
    headers = {**_bearer(token), "Idempotency-Key": "same"}
    async with _worker_pump(engine):
        created = await concurrent_client.post(
            COMPLETIONS, headers=headers, json=_body("once")
        )
        replayed = await concurrent_client.post(
            COMPLETIONS, headers=headers, json=_body("once")
        )
        conflict = await concurrent_client.post(
            COMPLETIONS, headers=headers, json=_body("once", "different")
        )
    assert created.status_code == 200
    assert replayed.status_code == 200
    assert created.json() == replayed.json()
    assert conflict.status_code == 409
    assert (
        cast(dict[str, Any], conflict.json())["error"]["code"]
        == "idempotency_key_reused"
    )
    assert await _count_runs(engine) == 1
