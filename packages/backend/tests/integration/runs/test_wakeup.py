import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier

UNREACHABLE = "redis://127.0.0.1:6399/0"


@pytest.fixture
async def notifier(settings: Any) -> AsyncIterator[RedisWakeUpNotifier]:
    """A live wake-up channel, or a skip.

    These tests assert the optimization itself, so they need a reachable Redis.
    The degraded CI run points ``REDIS_URL`` at an unused port deliberately;
    what has to keep passing there is every other test, not these.
    """
    # ``redis.asyncio`` ships no annotations for these two calls, the same gap
    # ``RedisWakeUpNotifier`` works around.
    client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url, socket_connect_timeout=1
    )
    try:
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        pytest.skip("no reachable wake-up channel")
    finally:
        await client.aclose()
    value = RedisWakeUpNotifier(settings.redis_url)
    yield value
    await value.close()


@pytest.fixture
async def broken_notifier() -> AsyncIterator[RedisWakeUpNotifier]:
    value = RedisWakeUpNotifier(UNREACHABLE)
    yield value
    await value.close()


def _worker(
    engine: AsyncEngine, notifier: Any, workspace_id: str | None = None
) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=notifier,
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=None if workspace_id is None else UUID(workspace_id),
        ),
    )


async def test_a_published_wake_up_releases_a_waiting_worker(
    notifier: RedisWakeUpNotifier,
) -> None:
    waiting = asyncio.create_task(notifier.wait(5))
    await asyncio.sleep(0.2)

    await notifier.publish(uuid4(), uuid4())

    assert await asyncio.wait_for(waiting, timeout=5) is True


async def test_waiting_times_out_when_nothing_is_published(
    notifier: RedisWakeUpNotifier,
) -> None:
    assert await notifier.wait(0.3) is False


async def test_a_wake_up_carries_only_identifiers(
    notifier: RedisWakeUpNotifier,
) -> None:
    workspace_id, run_id = uuid4(), uuid4()
    received: list[dict[str, str]] = []

    async def collect() -> None:
        if await notifier.wait(5):
            received.extend(notifier.drain())

    waiting = asyncio.create_task(collect())
    await asyncio.sleep(0.2)
    await notifier.publish(workspace_id, run_id)
    await asyncio.wait_for(waiting, timeout=5)

    assert received == [{"workspace_id": str(workspace_id), "run_id": str(run_id)}]


async def test_publishing_to_an_unreachable_redis_is_swallowed(
    broken_notifier: RedisWakeUpNotifier,
) -> None:
    await broken_notifier.publish(uuid4(), uuid4())

    assert await broken_notifier.wait(0.3) is False


async def test_the_platform_still_runs_with_redis_unreachable(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    broken_notifier: RedisWakeUpNotifier,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """Redis is a latency optimization, never a dependency."""
    session_id = session_for(agent_with_scenario("complete"))
    run = submit_run(session_id, "key-1")

    executed = await _worker(engine, broken_notifier).run_once()

    assert executed == UUID(str(run["id"]))
    assert client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()["status"] == (
        "completed"
    )


async def test_run_creation_publishes_after_the_transaction_commits(
    client: TestClient,
    scope: dict[str, str],
    session_id: str,
    notifier: RedisWakeUpNotifier,
) -> None:
    assert await notifier.wait(0.2) is False

    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "key-1"},
        json={"session_id": session_id, "input": "hello"},
    )
    assert created.status_code == 201

    assert await notifier.wait(5) is True
    assert notifier.drain() == [
        {
            "workspace_id": scope["X-Workspace-Id"],
            "run_id": str(created.json()["id"]),
        }
    ]


async def test_a_notification_for_an_unclaimable_run_is_harmless(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    notifier: RedisWakeUpNotifier,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    session_id = session_for(agent_with_scenario("complete"))
    run = submit_run(session_id, "key-1")
    worker = _worker(engine, notifier)
    await worker.run_once()

    await notifier.publish(UUID(scope["X-Workspace-Id"]), UUID(str(run["id"])))

    assert await worker.run_once() is None
    assert client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()["status"] == (
        "completed"
    )
