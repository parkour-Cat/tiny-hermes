"""Cross-Session claim order is the time a Run last entered the queue (§21.3, v2.13).

Claimed by `created_at`, a long task back from its time slice was always
older than a message that had just arrived, so a saturated deployment made
new work wait until every older long task had gone round again.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier


def _worker(
    engine: AsyncEngine, workspace_id: str, delay_ms: int = 0, slice_seconds: int = 30
) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=delay_ms),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=slice_seconds,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


async def _status(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": UUID(run_id)}
            )
        ).one()
    return str(row.status)


async def test_a_run_back_from_its_slice_queues_behind_one_that_arrived_meanwhile(
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    # `continue_once` needs two rounds. A slice ends when its time is up, not
    # at a round boundary, so a 1s slice and a 1.5s model round put the long
    # task back in the queue after its first round.
    agent = agent_with_scenario("continue_once")
    long_task = str(submit_run(session_for(agent), "long-task")["id"])
    worker = _worker(engine, scope["X-Workspace-Id"], delay_ms=1_500, slice_seconds=1)

    slice_one = asyncio.create_task(worker.run_once())
    for _ in range(250):
        if await _status(engine, long_task) == "running":
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the long task never started running")
    # Arrives while the long task holds the only Worker.
    message = str(submit_run(session_for(agent), "new-message")["id"])
    assert await slice_one == UUID(long_task)
    assert await _status(engine, long_task) == "queued", "the premise: slice one re-queued it"

    # The long task is older, but the message entered the queue first.
    assert await worker.run_once() == UUID(message)


async def test_a_run_waiting_behind_its_session_head_keeps_its_place(
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """Becoming head is not entering the queue: it has been queued all along.

    This held before v2.13 too. It is here so the new order cannot send a Run
    that waited behind its Session's head to the back when its turn comes.
    """
    agent = agent_with_scenario("complete")
    first_session = session_for(agent)
    head = str(submit_run(first_session, "head")["id"])
    behind_head = str(submit_run(first_session, "behind-head")["id"])
    later = str(submit_run(session_for(agent), "later")["id"])
    worker = _worker(engine, scope["X-Workspace-Id"])

    assert await worker.run_once() == UUID(head)
    assert await worker.run_once() == UUID(behind_head)
    assert await worker.run_once() == UUID(later)

