"""A Run that finishes on its last allowed model call completes (§12.3, v2.13).

Seen 2026-09-29: a long task whose final, goal-meeting call was its last
allowed one ended `paused(limit)` with `goal_verdict: done` in its history.
A paused Run keeps its Session's head, so the next message in that Session
sat behind a finished task until somebody widened a budget it did not need.
"""

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

from ..conftest import VALID_SPEC


def _agent_with_two_calls(client: TestClient, scope: dict[str, str]) -> str:
    """`continue_once` needs exactly two model calls; this Agent allows two."""
    agent_id = str(
        client.post(
            "/api/v1/agents",
            headers=scope,
            json={"name": "Exact", "alias": "exact-budget"},
        ).json()["id"]
    )
    spec = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "continue_once"},
        "limits": {**VALID_SPEC["limits"], "max_model_calls": 2},
    }
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    assert draft.status_code == 200
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201
    return agent_id


def _worker(engine: AsyncEngine, workspace_id: str) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


async def _status_and_events(engine: AsyncEngine, run_id: str) -> tuple[str, list[str]]:
    async with engine.connect() as connection:
        status = (
            await connection.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": UUID(run_id)}
            )
        ).scalar_one()
        events = (
            await connection.execute(
                text("SELECT event_type FROM run_events WHERE run_id = :id ORDER BY sequence"),
                {"id": UUID(run_id)},
            )
        ).scalars()
        return str(status), [str(event) for event in events]


async def test_a_run_finished_on_its_last_allowed_call_completes_and_frees_the_session(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session = session_for(_agent_with_two_calls(client, scope))
    finishing = str(submit_run(session, "finishing")["id"])
    next_message = str(submit_run(session, "next-message")["id"])
    worker = _worker(engine, scope["X-Workspace-Id"])

    assert await worker.run_once() == UUID(finishing)

    status, events = await _status_and_events(engine, finishing)
    assert status == "completed"
    assert "run_limit_reached" not in events
    # And the Session is free: the next message is claimed, not stuck behind it.
    assert await worker.run_once() == UUID(next_message)
