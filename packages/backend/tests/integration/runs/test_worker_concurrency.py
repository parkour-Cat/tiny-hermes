"""Several Workers in one event loop execute Runs at the same time.

`WORKER_CONCURRENCY` puts K `WorkerRuntime`s in one process, sharing one
database pool and one event loop. That is only worth anything if they really
overlap — a shared resource that serialized them would turn K Workers back
into one while every Run still completed — and only safe if each Run still
completes exactly as it would alone.

This pins behavior the process wiring relies on rather than driving new code:
it passed before `WORKER_CONCURRENCY` existed, because nothing in a Worker is
shared through module state. It is here so that stops being true loudly.
"""

import asyncio
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

WORKERS = 3
#: Long enough that three one-second model calls run back to back could not
#: be mistaken for three at once.
MODEL_DELAY_MS = 1_000


async def _times(engine: AsyncEngine, run_ids: list[str]) -> list[Any]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT id, status, started_at, finished_at FROM runs "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": [UUID(run_id) for run_id in run_ids]},
        )
        return list(rows.all())


async def test_workers_sharing_a_loop_and_a_pool_execute_runs_at_once(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    agent = agent_with_scenario("complete")
    # One Session per Run: Runs in one Session are serialized on purpose.
    run_ids = [
        str(submit_run(session_for(agent), f"concurrent-{index}")["id"])
        for index in range(WORKERS)
    ]
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workers = [
        WorkerRuntime(
            session_factory=sessions,
            model=DeterministicModelProvider(delay_ms=MODEL_DELAY_MS),
            notifier=NullWakeUpNotifier(),
            settings=WorkerSettings(
                worker_id=f"worker-{index}",
                lease_seconds=30,
                max_slice_seconds=30,
                idle_poll_seconds=1,
                workspace_id=UUID(workspace_id),
            ),
        )
        for index in range(WORKERS)
    ]
    stop = asyncio.Event()

    async def until_settled() -> None:
        while True:
            rows = await _times(engine, run_ids)
            if all(row.finished_at is not None for row in rows):
                stop.set()
                return
            await asyncio.sleep(0.1)

    await asyncio.wait_for(
        asyncio.gather(*(worker.run_forever(stop) for worker in workers), until_settled()),
        timeout=30,
    )

    rows = await _times(engine, run_ids)
    assert [row.status for row in rows] == ["completed"] * WORKERS
    # Every Run had started before any of them finished: all three were
    # executing at the same instant.
    assert max(row.started_at for row in rows) < min(row.finished_at for row in rows)
