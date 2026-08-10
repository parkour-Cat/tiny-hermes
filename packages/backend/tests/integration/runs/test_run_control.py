import asyncio
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.domain.models import RunCapabilities, RunEventType, RunSignal, RunState
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import (
    AppendEventsCommand,
    ApplySignalCommand,
    ReservedEvent,
)

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


async def test_independent_writers_reserve_contiguous_event_sequences(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    workspace_id = UUID(scope["X-Workspace-Id"])
    run_id = UUID(str(run["id"]))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def reserve(writer: str) -> None:
        async with factory.begin() as session:
            await SqlRunStore(session).append_events(
                AppendEventsCommand(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    events=(
                        ReservedEvent(RunEventType.RUN_SLICE_ENDED, {"writer": writer}),
                        ReservedEvent(RunEventType.RUN_SLICE_ENDED, {"writer": writer}),
                    ),
                )
            )

    await asyncio.gather(reserve("api"), reserve("worker"), reserve("scheduler"))

    async with engine.connect() as connection:
        sequences = (
            await connection.execute(
                text("SELECT sequence FROM run_events WHERE run_id = :run ORDER BY sequence"),
                {"run": run_id},
            )
        ).scalars().all()
        next_sequence = (
            await connection.execute(
                text("SELECT next_event_sequence FROM runs WHERE id = :run"),
                {"run": run_id},
            )
        ).scalar_one()

    assert list(sequences) == [1, 2, 3, 4, 5, 6, 7]
    assert next_sequence == 8


async def test_queued_pause_resume_and_cancel_use_state_versions(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    run_id = run["id"]

    paused = client.post(
        f"/api/v1/runs/{run_id}/pause", headers=scope, json={"expected_state_version": 1}
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert paused.json()["pause_reason"] == "manual"
    assert paused.json()["state_version"] == 2
    assert paused.json()["available_actions"] == ["resume", "cancel"]

    stale = client.post(
        f"/api/v1/runs/{run_id}/resume", headers=scope, json={"expected_state_version": 1}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "state_version_conflict"

    resumed = client.post(
        f"/api/v1/runs/{run_id}/resume", headers=scope, json={"expected_state_version": 2}
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"
    assert resumed.json()["state_version"] == 3
    assert resumed.json()["pause_reason"] is None

    cancelled = client.post(
        f"/api/v1/runs/{run_id}/cancel", headers=scope, json={"expected_state_version": 3}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["available_actions"] == []
    assert cancelled.json()["queue"] == {"position": 0, "status": "terminal"}

    async with engine.connect() as connection:
        events = (
            await connection.execute(
                text(
                    "SELECT event_type FROM run_events WHERE run_id = :run ORDER BY sequence"
                ),
                {"run": UUID(str(run_id))},
            )
        ).scalars().all()
        head = (
            await connection.execute(
                text("SELECT head_run_id FROM sessions WHERE id = :id"),
                {"id": UUID(session_id)},
            )
        ).scalar_one()
    assert list(events) == [
        "run_created",
        "run_pause_requested",
        "run_resume_requested",
        "run_cancel_requested",
    ]
    assert head is None


async def test_illegal_control_is_refused_and_audited(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    run_id = run["id"]
    client.post(
        f"/api/v1/runs/{run_id}/cancel", headers=scope, json={"expected_state_version": 1}
    )

    denied = client.post(
        f"/api/v1/runs/{run_id}/resume", headers=scope, json={"expected_state_version": 2}
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "invalid_state_transition"

    async with engine.connect() as connection:
        denials = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action = 'run.control_denied' AND result = 'denied'"
                )
            )
        ).scalar_one()
        state = (
            await connection.execute(
                text("SELECT status, state_version FROM runs WHERE id = :id"),
                {"id": UUID(str(run_id))},
            )
        ).one()
    assert denials == 1
    assert state[0] == "cancelled"
    assert state[1] == 2


async def test_terminal_head_hands_off_across_a_cancelled_pending_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    first = _submit(client, scope, session_id, "key-1")
    second = _submit(client, scope, session_id, "key-2")
    third = _submit(client, scope, session_id, "key-3")

    await _apply(engine, scope["X-Workspace-Id"], str(first["id"]), RunSignal.LEASE_ACQUIRED)

    cancelled = client.post(
        f"/api/v1/runs/{second['id']}/cancel",
        headers=scope,
        json={"expected_state_version": 1},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    await _apply(engine, scope["X-Workspace-Id"], str(first["id"]), RunSignal.COMPLETED)

    session = client.get(f"/api/v1/sessions/{session_id}", headers=scope).json()
    assert session["head_run_id"] == third["id"]

    reloaded_second = client.get(f"/api/v1/runs/{second['id']}", headers=scope).json()
    reloaded_third = client.get(f"/api/v1/runs/{third['id']}", headers=scope).json()
    assert reloaded_second["status"] == "cancelled"
    assert reloaded_third["status"] == "queued"
    assert reloaded_third["blocked_by_run_id"] is None
    assert reloaded_third["queue"] == {"position": 1, "status": "head"}


async def test_a_terminal_pending_run_does_not_move_the_head(
    client: TestClient, scope: dict[str, str], session_id: str
) -> None:
    first = _submit(client, scope, session_id, "key-1")
    second = _submit(client, scope, session_id, "key-2")

    client.post(
        f"/api/v1/runs/{second['id']}/cancel",
        headers=scope,
        json={"expected_state_version": 1},
    )

    session = client.get(f"/api/v1/sessions/{session_id}", headers=scope).json()
    assert session["head_run_id"] == first["id"]


async def test_running_pause_and_cancel_only_record_requests(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    await _apply(engine, scope["X-Workspace-Id"], str(run["id"]), RunSignal.LEASE_ACQUIRED)

    current = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert current["status"] == "running"

    paused = client.post(
        f"/api/v1/runs/{run['id']}/pause",
        headers=scope,
        json={"expected_state_version": current["state_version"]},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "running"
    assert paused.json()["available_actions"] == ["cancel"]

    cancelled = client.post(
        f"/api/v1/runs/{run['id']}/cancel",
        headers=scope,
        json={"expected_state_version": paused.json()["state_version"]},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "running"
    assert cancelled.json()["available_actions"] == []

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT pause_requested_at IS NOT NULL, cancel_requested_at IS NOT NULL "
                    "FROM runs WHERE id = :id"
                ),
                {"id": UUID(str(run["id"]))},
            )
        ).one()
    assert tuple(row) == (True, True)


async def test_viewers_cannot_control_a_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE workspace_id = :id"),
            {"id": UUID(scope["X-Workspace-Id"])},
        )

    denied = client.post(
        f"/api/v1/runs/{run['id']}/pause", headers=scope, json={"expected_state_version": 1}
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"

    listed = client.get(f"/api/v1/runs/{run['id']}", headers=scope)
    assert listed.status_code == 200
    assert listed.json()["available_actions"] == []


@pytest.mark.parametrize("signal", [RunSignal.COMPLETED, RunSignal.FAILED])
async def test_apply_signal_is_the_only_terminal_seam(
    client: TestClient,
    scope: dict[str, str],
    session_id: str,
    engine: AsyncEngine,
    signal: RunSignal,
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    await _apply(engine, scope["X-Workspace-Id"], str(run["id"]), RunSignal.LEASE_ACQUIRED)
    await _apply(engine, scope["X-Workspace-Id"], str(run["id"]), signal)

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == RunState(signal.value).value
    assert reloaded["finished_at"] is not None
    assert reloaded["queue"] == {"position": 0, "status": "terminal"}

    async with engine.connect() as connection:
        expiries = (
            await connection.execute(
                text("SELECT count(*) FROM idempotency_records WHERE expires_at IS NOT NULL")
            )
        ).scalar_one()
    assert expiries == 1


async def test_control_of_a_cross_workspace_run_is_a_generic_not_found(
    client: TestClient, scope: dict[str, str], session_id: str, admin_csrf: str
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    other = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Secondary"},
    ).json()

    crossed = client.post(
        f"/api/v1/runs/{run['id']}/pause",
        headers={"X-Workspace-Id": other["id"], "X-CSRF-Token": admin_csrf},
        json={"expected_state_version": 1},
    )
    assert crossed.status_code == 404
    assert crossed.json()["code"] == "run_not_found"
