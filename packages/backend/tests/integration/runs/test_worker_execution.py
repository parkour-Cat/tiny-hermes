from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse


class CountingProvider:
    """Counts real provider calls without replacing the shipped behavior."""

    def __init__(self) -> None:
        self._inner = DeterministicModelProvider(delay_ms=0)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return await self._inner.complete(request)


class InterleavingProvider:
    """Runs the shipped provider, then performs a side effect mid-round.

    This is how a control request that arrives while the model is working is
    reproduced deterministically.
    """

    def __init__(self, during: Callable[[], None]) -> None:
        self._inner = DeterministicModelProvider(delay_ms=0)
        self._during = during

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._inner.complete(request)
        self._during()
        return response


def _worker(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    model: Any = None,
    max_slice_seconds: int = 30,
    worker_id: str = "worker-a",
) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model or DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=30,
            max_slice_seconds=max_slice_seconds,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


async def _events(engine: AsyncEngine, run_id: Any) -> list[str]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    text(
                        "SELECT event_type FROM run_events WHERE run_id = :id "
                        "ORDER BY sequence"
                    ),
                    {"id": UUID(str(run_id))},
                )
            )
            .scalars()
            .all()
        )


async def _budget(engine: AsyncEngine, root_run_id: Any) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT consumed_execution_ms, consumed_model_calls, consumed_tokens "
                    "FROM run_budget_scopes WHERE root_run_id = :id"
                ),
                {"id": UUID(str(root_run_id))},
            )
        ).one()
    return (int(row[0]), int(row[1]), int(row[2]))


async def test_a_complete_run_finishes_without_any_manual_signal(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("complete"))
    run = submit_run(session_id, "key-1")

    executed = await _worker(engine, scope["X-Workspace-Id"]).run_once()

    assert executed == UUID(str(run["id"]))
    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "completed"
    assert reloaded["started_at"] is not None
    assert reloaded["finished_at"] is not None
    assert reloaded["available_actions"] == []
    assert await _events(engine, run["id"]) == [
        "run_created",
        "run_lease_acquired",
        "run_completed",
        "goal_verdict",
    ]

    async with engine.connect() as connection:
        open_leases = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM worker_leases WHERE released_at IS NULL"
                )
            )
        ).scalar_one()
    assert open_leases == 0

    executed_ms, model_calls, tokens = await _budget(
        engine, run["budget_root_run_id"]
    )
    assert model_calls == 1
    assert tokens > 0
    assert executed_ms >= 0


async def test_continue_once_crosses_a_slice_boundary(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    run = submit_run(session_id, "key-1")
    # A one-round slice forces the boundary the product calls an execution slice.
    worker = _worker(engine, scope["X-Workspace-Id"], max_slice_seconds=0)

    assert await worker.run_once() == UUID(str(run["id"]))
    after_first = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert after_first["status"] == "queued"
    assert after_first["queue"] == {"position": 1, "status": "head"}

    async with engine.connect() as connection:
        released = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM worker_leases "
                    "WHERE run_id = :id AND released_at IS NOT NULL"
                ),
                {"id": UUID(str(run["id"]))},
            )
        ).scalar_one()
    assert released == 1

    assert await worker.run_once() == UUID(str(run["id"]))
    after_second = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert after_second["status"] == "completed"

    assert await _events(engine, run["id"]) == [
        "run_created",
        "run_lease_acquired",
        "run_slice_ended",
        "goal_verdict",
        "run_lease_acquired",
        "run_completed",
        "goal_verdict",
    ]
    _, model_calls, _ = await _budget(engine, run["budget_root_run_id"])
    assert model_calls == 2


async def test_a_replay_safe_failure_can_be_retried_and_executed(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("fail_replay_safe"))
    run = submit_run(session_id, "key-1")
    worker = _worker(engine, scope["X-Workspace-Id"])

    await worker.run_once()

    failed = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert failed["status"] == "failed"
    assert failed["checkpoint_replay_safe"] is True
    assert failed["checkpoint_effect_status"] == "none"
    assert failed["available_actions"] == ["retry"]

    derived = client.post(
        f"/api/v1/runs/{run['id']}/retry",
        headers={**scope, "Idempotency-Key": "retry-1"},
        json={},
    )
    assert derived.status_code == 201
    assert derived.json()["budget_root_run_id"] == run["budget_root_run_id"]

    await worker.run_once()
    retried = client.get(f"/api/v1/runs/{derived.json()['id']}", headers=scope).json()
    assert retried["status"] == "failed"
    _, model_calls, _ = await _budget(engine, run["budget_root_run_id"])
    assert model_calls == 2


async def test_a_running_pause_takes_effect_at_the_next_checkpoint(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    run = submit_run(session_id, "key-1")
    worker = _worker(engine, scope["X-Workspace-Id"], max_slice_seconds=0)

    await worker.run_once()
    requeued = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert requeued["status"] == "queued"

    paused = client.post(
        f"/api/v1/runs/{run['id']}/pause",
        headers=scope,
        json={"expected_state_version": requeued["state_version"]},
    )
    assert paused.json()["status"] == "paused"
    assert paused.json()["pause_reason"] == "manual"

    assert await worker.run_once() is None


async def test_a_pause_requested_while_running_pauses_after_the_round(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    run = submit_run(session_id, "key-1")

    def pause_now() -> None:
        client.post(
            f"/api/v1/runs/{run['id']}/pause",
            headers=scope,
            json={"expected_state_version": 2},
        )

    worker = _worker(
        engine, scope["X-Workspace-Id"], model=InterleavingProvider(pause_now)
    )

    await worker.run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "manual"
    assert await _events(engine, run["id"]) == [
        "run_created",
        "run_lease_acquired",
        "run_pause_requested",
        "run_safe_pause_reached",
        "goal_verdict",
    ]


async def test_a_cancel_requested_while_running_cancels_at_the_checkpoint(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    first = submit_run(session_id, "key-1")
    second = submit_run(session_id, "key-2")

    def cancel_now() -> None:
        client.post(
            f"/api/v1/runs/{first['id']}/cancel",
            headers=scope,
            json={"expected_state_version": 2},
        )

    worker = _worker(
        engine, scope["X-Workspace-Id"], model=InterleavingProvider(cancel_now)
    )

    await worker.run_once()

    reloaded = client.get(f"/api/v1/runs/{first['id']}", headers=scope).json()
    assert reloaded["status"] == "cancelled"
    assert await _events(engine, first["id"]) == [
        "run_created",
        "run_lease_acquired",
        "run_cancel_requested",
        "run_safe_cancel_started",
        "goal_verdict",
        "run_safe_cancel_finished",
    ]
    session = client.get(f"/api/v1/sessions/{session_id}", headers=scope).json()
    assert session["head_run_id"] == second["id"]


async def test_the_safety_valve_stops_a_run_before_another_model_call(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("continue_once"))
    run = submit_run(session_id, "key-1")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_budget_scopes "
                "SET consumed_model_calls = max_model_calls - 1 WHERE root_run_id = :id"
            ),
            {"id": UUID(str(run["budget_root_run_id"]))},
        )
    provider = CountingProvider()
    worker = _worker(engine, scope["X-Workspace-Id"], model=provider)

    await worker.run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "limit"
    assert reloaded["available_actions"] == ["cancel"]
    assert provider.calls == 1
    assert await _events(engine, run["id"]) == [
        "run_created",
        "run_lease_acquired",
        "run_safe_pause_reached",
        "run_limit_reached",
        "goal_verdict",
    ]


async def test_nothing_claimable_is_not_an_error(
    scope: dict[str, str], engine: AsyncEngine, session_id: str
) -> None:
    del session_id
    assert await _worker(engine, scope["X-Workspace-Id"]).run_once() is None


async def test_only_the_head_run_executes_and_the_queue_drains_in_order(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("complete"))
    runs = [submit_run(session_id, f"key-{index}") for index in range(1, 4)]
    worker = _worker(engine, scope["X-Workspace-Id"])

    executed = [await worker.run_once() for _ in range(3)]

    assert executed == [UUID(str(run["id"])) for run in runs]
    assert await worker.run_once() is None
    listed = client.get(f"/api/v1/runs?session_id={session_id}", headers=scope).json()
    assert [item["status"] for item in listed] == ["completed"] * 3
    session = client.get(f"/api/v1/sessions/{session_id}", headers=scope).json()
    assert session["head_run_id"] is None
