"""Capacity plan item 5: a watched Run is read when it has news, not twice a second.

The write side announces a Run once the transaction holding its events has
committed; the API's event hub holds its rooms until it hears one, or until a
long fallback interval passes. Redis carries the announcement and decides
nothing: without it every room polls as it did before.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

import httpx2 as httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.domain.models import RunEventType
from tiny_hermes.runs.infrastructure.event_push import RedisRunEventPush, announcing
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import AppendEventsCommand, ReservedEvent
from tiny_hermes.shared.config import Settings

from integration.support import EventsUrl

UNREACHABLE = "redis://127.0.0.1:6399/0"


@pytest.fixture
async def reachable_redis(settings: Settings) -> AsyncIterator[str]:
    """The URL of a live Redis, or a skip — the degraded CI run has none."""
    client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url, socket_connect_timeout=1
    )
    try:
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        pytest.skip("no reachable push channel")
    finally:
        await client.aclose()
    yield settings.redis_url


@pytest.fixture
def watched(submitted_run: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """A queued Run nobody will claim: its only news is what a test appends."""
    return {"id": submitted_run["id"], "workspace_id": workspace_id}


async def _append(
    engine: AsyncEngine,
    run: dict[str, Any],
    announce: Callable[[frozenset[UUID]], None] | None = None,
) -> None:
    info = {} if announce is None else announcing(announce)
    factory = async_sessionmaker(engine, expire_on_commit=False, info=info)
    async with factory.begin() as session:
        await SqlRunStore(session).append_events(_command(run))


def _command(run: dict[str, Any]) -> AppendEventsCommand:
    return AppendEventsCommand(
        workspace_id=UUID(run["workspace_id"]),
        run_id=UUID(run["id"]),
        events=(ReservedEvent(event_type=RunEventType.GOAL_VERDICT, payload={}),),
    )


# -- the write side -------------------------------------------------------


async def test_a_run_is_announced_once_its_events_have_committed(
    engine: AsyncEngine, watched: dict[str, Any]
) -> None:
    heard: list[frozenset[UUID]] = []
    factory = async_sessionmaker(engine, expire_on_commit=False, info=announcing(heard.append))

    async with factory.begin() as session:
        await SqlRunStore(session).append_events(_command(watched))
        # Before the commit a reader would find nothing and go back to sleep.
        assert heard == []

    assert heard == [frozenset({UUID(watched["id"])})]


async def test_events_that_were_rolled_back_are_not_announced(
    engine: AsyncEngine, watched: dict[str, Any]
) -> None:
    heard: list[frozenset[UUID]] = []
    factory = async_sessionmaker(engine, expire_on_commit=False, info=announcing(heard.append))

    async with factory() as session:
        await SqlRunStore(session).append_events(_command(watched))
        await session.rollback()
        # The same session goes on to commit something else.
        await session.commit()

    assert heard == []


async def test_a_commit_without_events_announces_nothing(
    engine: AsyncEngine, watched: dict[str, Any]
) -> None:
    heard: list[frozenset[UUID]] = []
    factory = async_sessionmaker(engine, expire_on_commit=False, info=announcing(heard.append))

    async with factory.begin() as session:
        await SqlRunStore(session).event_window(
            UUID(watched["workspace_id"]), UUID(watched["id"])
        )

    assert heard == []


async def test_a_failing_announcement_does_not_fail_the_commit(
    engine: AsyncEngine, watched: dict[str, Any]
) -> None:
    def broken(_: frozenset[UUID]) -> None:
        raise RuntimeError("the channel is gone")

    await _append(engine, watched, broken)


# -- the channel ----------------------------------------------------------


async def test_an_announced_run_reaches_a_listener(reachable_redis: str) -> None:
    push = RedisRunEventPush(reachable_redis)
    healthy = asyncio.Event()
    heard: list[UUID] = []
    arrived = asyncio.Event()
    run_id = UUID("00000000-0000-0000-0000-00000000a5a5")

    def on_run(value: UUID) -> None:
        heard.append(value)
        arrived.set()

    listener = asyncio.create_task(
        push.listen(on_run, lambda up: healthy.set() if up else healthy.clear())
    )
    try:
        await asyncio.wait_for(healthy.wait(), timeout=5)
        push.announce(frozenset({run_id}))
        await asyncio.wait_for(arrived.wait(), timeout=5)
    finally:
        listener.cancel()
        await push.close()

    assert heard == [run_id]


async def test_an_unreachable_channel_is_reported_and_never_raises() -> None:
    push = RedisRunEventPush(UNREACHABLE)
    health: list[bool] = []
    listener = asyncio.create_task(push.listen(lambda _: None, health.append))
    try:
        push.announce(frozenset({UUID("00000000-0000-0000-0000-00000000a5a5")}))
        await asyncio.sleep(1.5)
        assert not listener.done()
    finally:
        listener.cancel()
        await push.close()

    assert True not in health


# -- through the API ------------------------------------------------------


async def _live_frames(
    browser: httpx.AsyncClient, url: str, into: list[str], ready: asyncio.Event
) -> None:
    async with browser.stream("GET", url) as response:
        assert response.status_code == 200
        async for event in httpx.EventSource(response).aiter_sse():
            into.append(event.event)
            ready.set()


async def _wait_for(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_an_announced_event_reaches_a_subscriber_at_once(
    reachable_redis: str,
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    events_url: EventsUrl,
    watched: dict[str, Any],
) -> None:
    push = RedisRunEventPush(reachable_redis)
    seen: list[str] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(_live_frames(browser, events_url(watched["id"]), seen, ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        await asyncio.sleep(1.0)  # the room is holding, and has heard Redis say hello

        await _append(engine, watched, push.announce)
        assert await _wait_for(lambda: "goal_verdict" in seen, timeout=1.0)
    finally:
        reader.cancel()
        await push.close()


async def test_an_unannounced_event_waits_for_the_fallback_read(
    reachable_redis: str,
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    events_url: EventsUrl,
    watched: dict[str, Any],
) -> None:
    """Proves the room really holds: polling at 0.5 s would deliver this in 1 s."""
    del reachable_redis
    seen: list[str] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(_live_frames(browser, events_url(watched["id"]), seen, ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        await asyncio.sleep(1.0)

        await _append(engine, watched)  # a writer whose announcement was lost
        assert not await _wait_for(lambda: "goal_verdict" in seen, timeout=1.0)
        assert await _wait_for(lambda: "goal_verdict" in seen, timeout=10.0)
    finally:
        reader.cancel()


@pytest.mark.settings(redis_url=UNREACHABLE)
async def test_without_redis_a_subscriber_waits_no_longer_than_before(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    events_url: EventsUrl,
    watched: dict[str, Any],
) -> None:
    seen: list[str] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(_live_frames(browser, events_url(watched["id"]), seen, ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        await asyncio.sleep(1.0)

        await _append(engine, watched)
        assert await _wait_for(lambda: "goal_verdict" in seen, timeout=1.5)
    finally:
        reader.cancel()
