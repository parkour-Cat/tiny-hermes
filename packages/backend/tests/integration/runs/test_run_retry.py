import asyncio
from uuid import UUID

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.domain.models import RunCapabilities, RunSignal
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import ApplySignalCommand

FULL = RunCapabilities(can_control=True, can_retry=True)


def _submit(
    client: TestClient, scope: dict[str, str], session_id: str, key: str
) -> dict[str, object]:
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201
    return dict(response.json())


async def _apply(
    engine: AsyncEngine, workspace_id: str, run_id: str, signal: RunSignal
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        await RunCoordination(SqlRunStore(session)).apply_signal(
            ApplySignalCommand(
                workspace_id=UUID(workspace_id),
                run_id=UUID(run_id),
                signal=signal,
                request_id=f"signal-{signal.value}",
                capabilities=FULL,
            )
        )


async def _fail(engine: AsyncEngine, workspace_id: str, run_id: str) -> None:
    """Drive a Run to a replay-safe failure through the state seam only."""
    await _apply(engine, workspace_id, run_id, RunSignal.LEASE_ACQUIRED)
    await _apply(engine, workspace_id, run_id, RunSignal.FAILED)


def _retry(
    client: TestClient, scope: dict[str, str], run_id: str, key: str
) -> httpx.Response:
    return client.post(
        f"/api/v1/runs/{run_id}/retry", headers={**scope, "Idempotency-Key": key}, json={}
    )


@pytest.fixture
async def failed_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> dict[str, object]:
    run = _submit(client, scope, session_id, "key-1")
    await _fail(engine, scope["X-Workspace-Id"], str(run["id"]))
    return run


async def test_a_failed_run_advertises_retry_and_derives_one(
    client: TestClient, scope: dict[str, str], failed_run: dict[str, object]
) -> None:
    reloaded = client.get(f"/api/v1/runs/{failed_run['id']}", headers=scope).json()
    assert reloaded["status"] == "failed"
    assert reloaded["available_actions"] == ["retry"]

    derived = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert derived.status_code == 201
    assert derived.headers["Location"].endswith(f"/api/v1/runs/{derived.json()['id']}")
    assert derived.json()["retry_of_run_id"] == failed_run["id"]
    assert derived.json()["budget_root_run_id"] == failed_run["budget_root_run_id"]
    assert derived.json()["session_sequence"] == 2
    assert derived.json()["status"] == "queued"
    assert derived.json()["budget"]["derived_retry_count"] == 1

    source = client.get(f"/api/v1/runs/{failed_run['id']}", headers=scope).json()
    assert source["status"] == "failed"
    assert source["available_actions"] == []


async def test_retry_requires_an_idempotency_key_and_replays_it(
    client: TestClient, scope: dict[str, str], failed_run: dict[str, object]
) -> None:
    missing = client.post(f"/api/v1/runs/{failed_run['id']}/retry", headers=scope, json={})
    assert missing.status_code == 400
    assert missing.json()["code"] == "idempotency_key_required"

    created = _retry(client, scope, str(failed_run["id"]), "retry-1")
    replay = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == created.json()


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("checkpoint_replay_safe", "false", "retry_not_safe"),
        ("checkpoint_effect_status", "'unknown'", "retry_not_safe"),
        ("checkpoint_workspace_revision_id", "gen_random_uuid()", "retry_context_stale"),
    ],
)
async def test_unsafe_checkpoints_reject_retry(
    client: TestClient,
    scope: dict[str, str],
    failed_run: dict[str, object],
    engine: AsyncEngine,
    column: str,
    value: str,
    code: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(f"UPDATE runs SET {column} = {value} WHERE id = :id"),  # noqa: S608
            {"id": UUID(str(failed_run["id"]))},
        )

    denied = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert denied.status_code == 409
    assert denied.json()["code"] == code
    await _assert_no_retry(engine)


async def test_a_source_that_is_not_the_latest_run_rejects_retry(
    client: TestClient,
    scope: dict[str, str],
    session_id: str,
    failed_run: dict[str, object],
    engine: AsyncEngine,
) -> None:
    _submit(client, scope, session_id, "key-2")

    denied = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert denied.status_code == 409
    assert denied.json()["code"] == "retry_context_stale"
    await _assert_no_retry(engine)


async def test_an_expired_elapsed_deadline_rejects_retry(
    client: TestClient,
    scope: dict[str, str],
    failed_run: dict[str, object],
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_budget_scopes SET elapsed_deadline_at = now() - interval '1 hour' "
                "WHERE root_run_id = :id"
            ),
            {"id": UUID(str(failed_run["budget_root_run_id"]))},
        )

    denied = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert denied.status_code == 409
    assert denied.json()["code"] == "retry_budget_exhausted"
    await _assert_no_retry(engine)


@pytest.mark.parametrize(
    "assignment",
    [
        "consumed_model_calls = max_model_calls",
        "consumed_tool_calls = max_tool_calls",
        "consumed_execution_ms = max_execution_seconds * 1000",
        "max_tokens = 100, consumed_tokens = 100",
    ],
)
async def test_an_exhausted_budget_rejects_retry(
    client: TestClient,
    scope: dict[str, str],
    failed_run: dict[str, object],
    engine: AsyncEngine,
    assignment: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE run_budget_scopes SET {assignment} WHERE root_run_id = :id"  # noqa: S608
            ),
            {"id": UUID(str(failed_run["budget_root_run_id"]))},
        )

    denied = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert denied.status_code == 409
    assert denied.json()["code"] == "retry_budget_exhausted"
    await _assert_no_retry(engine)


async def test_a_viewer_cannot_derive_a_retry(
    client: TestClient,
    scope: dict[str, str],
    failed_run: dict[str, object],
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE workspace_id = :id"),
            {"id": UUID(scope["X-Workspace-Id"])},
        )

    denied = _retry(client, scope, str(failed_run["id"]), "retry-1")

    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"
    await _assert_no_retry(engine)


async def test_a_retry_chain_shares_one_root_budget_and_stops_at_three(
    client: TestClient,
    scope: dict[str, str],
    session_id: str,
    failed_run: dict[str, object],
    concurrent_client: httpx.AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    root_budget = str(failed_run["budget_root_run_id"])

    first = _retry(client, scope, str(failed_run["id"]), "retry-1").json()
    await _fail(engine, workspace_id, str(first["id"]))
    second = _retry(client, scope, str(first["id"]), "retry-2").json()
    await _fail(engine, workspace_id, str(second["id"]))

    assert first["budget_root_run_id"] == root_budget
    assert second["budget_root_run_id"] == root_budget
    assert first["retry_of_run_id"] == failed_run["id"]
    assert second["retry_of_run_id"] == first["id"]
    assert second["budget"]["derived_retry_count"] == 2

    async with engine.connect() as connection:
        budgets = (
            await connection.execute(
                text("SELECT count(*) FROM run_budget_scopes WHERE root_run_id = :id"),
                {"id": UUID(root_budget)},
            )
        ).scalar_one()
        total_budgets = (
            await connection.execute(text("SELECT count(*) FROM run_budget_scopes"))
        ).scalar_one()
    assert budgets == 1
    assert total_budgets == 1

    responses = await asyncio.gather(
        *[
            concurrent_client.post(
                f"/api/v1/runs/{second['id']}/retry",
                headers={**scope, "Idempotency-Key": f"race-{index}"},
                json={},
            )
            for index in range(5)
        ]
    )
    created = [item for item in responses if item.status_code == 201]
    refused = [item for item in responses if item.status_code == 409]
    assert len(created) == 1
    assert len(refused) == 4
    assert {item.json()["code"] for item in refused} == {"retry_limit_reached"}

    third = created[0].json()
    assert third["budget"]["derived_retry_count"] == 3

    winning_key = next(
        f"race-{index}"
        for index in range(5)
        if responses[index].status_code == 201
    )
    replay = _retry(client, scope, str(second["id"]), winning_key)
    assert replay.status_code == 200
    assert replay.json()["id"] == third["id"]

    await _fail(engine, workspace_id, str(third["id"]))
    fourth = _retry(client, scope, str(third["id"]), "retry-4")
    assert fourth.status_code == 409
    assert fourth.json()["code"] == "retry_limit_reached"

    async with engine.connect() as connection:
        count = (
            await connection.execute(
                text("SELECT derived_retry_count FROM run_budget_scopes WHERE root_run_id = :id"),
                {"id": UUID(root_budget)},
            )
        ).scalar_one()
        runs = (
            await connection.execute(
                text("SELECT count(*) FROM runs WHERE session_id = :id"),
                {"id": UUID(session_id)},
            )
        ).scalar_one()
    assert count == 3
    assert runs == 4


async def _assert_no_retry(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM runs WHERE retry_of_run_id IS NOT NULL), "
                    "(SELECT coalesce(sum(derived_retry_count), 0) FROM run_budget_scopes)"
                )
            )
        ).one()
    assert tuple(int(value) for value in counts) == (0, 0)
