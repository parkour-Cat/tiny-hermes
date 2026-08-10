import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any
from uuid import UUID

import httpx2 as httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.api.app import create_app
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.shared.config import Settings

STREAM_TIMEOUT = 20


@contextlib.asynccontextmanager
async def _serving(settings: Settings) -> AsyncGenerator[str]:
    """Run the real API on a real socket.

    Every ASGI test transport available here buffers the whole response body
    before returning it, which would make a streaming assertion meaningless and
    a mid-stream disconnect impossible to express.
    """
    config = uvicorn.Config(
        create_app(settings=settings), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield f"http://127.0.0.1:{int(address[1])}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def live_server(settings: Settings, client: TestClient) -> AsyncIterator[str]:
    del client  # the bootstrap fixtures must prepare the same database first
    async with _serving(settings) as url:
        yield url


@pytest.fixture
async def browser(
    live_server: str, client: TestClient, scope: dict[str, str]
) -> AsyncIterator[httpx.AsyncClient]:
    """A subscriber carrying the already authenticated browser session."""
    del scope  # ordering only: the login must happen before the cookies are read
    cookies = {name: value for name, value in client.cookies.items()}
    async with httpx.AsyncClient(
        base_url=live_server, cookies=cookies, timeout=STREAM_TIMEOUT
    ) as value:
        yield value


def _worker(engine: AsyncEngine, max_slice_seconds: int = 30) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=max_slice_seconds,
            idle_poll_seconds=1,
        ),
    )


def _scheduler(engine: AsyncEngine) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(
            max_recovery_attempts=3, event_retention_hours=1, batch_size=50
        ),
    )


async def _drain(engine: AsyncEngine, max_slice_seconds: int = 30) -> None:
    worker = _worker(engine, max_slice_seconds)
    while await worker.run_once() is not None:
        pass


def _events_url(run_id: Any, workspace_id: str, cursor: int | None = None) -> str:
    url = f"/api/v1/runs/{run_id}/events?workspace_id={workspace_id}"
    return url if cursor is None else f"{url}&last_event_id={cursor}"


async def _collect(
    browser: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> list[httpx.ServerSentEvent]:
    """Read one stream to its end, as a browser EventSource would."""

    async def read() -> list[httpx.ServerSentEvent]:
        frames: list[httpx.ServerSentEvent] = []
        async with browser.stream("GET", url, headers=headers or {}) as response:
            assert response.status_code == 200
            async for frame in httpx.EventSource(response):
                frames.append(frame)
        return frames

    return await asyncio.wait_for(read(), timeout=STREAM_TIMEOUT)


AGE_ALL = text(
    "UPDATE run_events SET occurred_at = now() - interval '30 days' WHERE run_id = :id"
)
AGE_UPTO = text(
    "UPDATE run_events SET occurred_at = now() - interval '30 days' "
    "WHERE run_id = :id AND sequence <= :upto"
)


async def _age_events(engine: AsyncEngine, run_id: UUID, upto: int | None) -> None:
    """Push events out of the retention window as real elapsed time would."""
    async with engine.begin() as connection:
        if upto is None:
            await connection.execute(AGE_ALL, {"id": run_id})
        else:
            await connection.execute(AGE_UPTO, {"id": run_id, "upto": upto})


async def test_an_unauthenticated_subscriber_is_refused(
    live_server: str, scope: dict[str, str], submitted_run: dict[str, Any]
) -> None:
    async with httpx.AsyncClient(base_url=live_server, timeout=STREAM_TIMEOUT) as anon:
        refused = await anon.get(
            _events_url(submitted_run["id"], scope["X-Workspace-Id"])
        )

    assert refused.status_code == 401
    assert refused.json()["code"] == "unauthenticated"


async def test_a_missing_workspace_is_refused(
    browser: httpx.AsyncClient, submitted_run: dict[str, Any]
) -> None:
    refused = await browser.get(f"/api/v1/runs/{submitted_run['id']}/events")

    assert refused.status_code == 400
    assert refused.json()["code"] == "workspace_required"


async def test_a_cross_workspace_run_is_generically_not_found(
    browser: httpx.AsyncClient,
    client: TestClient,
    admin_csrf: str,
    submitted_run: dict[str, Any],
) -> None:
    other = client.post(
        "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}, json={"name": "Other"}
    )
    assert other.status_code == 201

    refused = await browser.get(
        _events_url(submitted_run["id"], str(other.json()["id"]))
    )

    assert refused.status_code == 404
    assert refused.json()["code"] == "run_not_found"
    body = refused.text.lower()
    assert "analyst" not in body
    assert "concise" not in body


async def test_a_finished_run_streams_every_event_then_closes(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    submitted_run: dict[str, Any],
) -> None:
    await _drain(engine)

    frames = await _collect(
        browser, _events_url(submitted_run["id"], scope["X-Workspace-Id"])
    )

    assert [frame.event for frame in frames] == [
        "run_created",
        "run_lease_acquired",
        "run_completed",
    ]
    assert [frame.id for frame in frames] == ["1", "2", "3"]
    assert frames[0].json() == {
        "sequence": 1,
        "event_type": "run_created",
        "occurred_at": frames[0].json()["occurred_at"],  # type: ignore[index]
        "payload": {"session_sequence": 1},
    }


async def test_resuming_from_a_cursor_replays_neither_more_nor_less(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    submitted_run: dict[str, Any],
) -> None:
    await _drain(engine)
    url = _events_url(submitted_run["id"], scope["X-Workspace-Id"])

    resumed = await _collect(browser, url, {"Last-Event-ID": "2"})

    assert [frame.id for frame in resumed] == ["3"]


async def test_the_cursor_query_matches_the_header_and_the_header_wins(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    submitted_run: dict[str, Any],
) -> None:
    await _drain(engine)
    workspace_id = scope["X-Workspace-Id"]

    queried = await _collect(browser, _events_url(submitted_run["id"], workspace_id, 1))
    both = await _collect(
        browser,
        _events_url(submitted_run["id"], workspace_id, 1),
        {"Last-Event-ID": "2"},
    )

    assert [frame.id for frame in queried] == ["2", "3"]
    assert [frame.id for frame in both] == ["3"]


async def test_a_cursor_below_the_retained_window_is_gone(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    submitted_run: dict[str, Any],
) -> None:
    run_id = UUID(str(submitted_run["id"]))
    await _drain(engine)
    await _age_events(engine, run_id, upto=2)
    await _scheduler(engine).run_once()

    refused = await browser.get(_events_url(run_id, scope["X-Workspace-Id"]))

    assert refused.status_code == 410
    assert refused.headers["content-type"].startswith("application/problem+json")
    body = refused.json()
    assert body["code"] == "event_cursor_too_old"
    assert body["context"]["earliest_available_sequence"] == 3
    assert body["context"]["run_url"] == f"/api/v1/runs/{run_id}"


async def test_a_fully_pruned_run_is_gone_rather_than_silently_empty(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    submitted_run: dict[str, Any],
) -> None:
    run_id = UUID(str(submitted_run["id"]))
    workspace_id = scope["X-Workspace-Id"]
    await _drain(engine)
    await _age_events(engine, run_id, upto=None)
    await _scheduler(engine).run_once()

    refused = await browser.get(_events_url(run_id, workspace_id))

    assert refused.status_code == 410
    assert refused.json()["context"]["earliest_available_sequence"] == 4
    # A subscriber that already saw everything is caught up, not stale.
    assert await _collect(browser, _events_url(run_id, workspace_id, 3)) == []


async def test_a_live_run_streams_until_it_terminalizes(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    run = submit_run(session_for(agent_with_scenario("continue_once")), "key-1")
    url = _events_url(run["id"], scope["X-Workspace-Id"])

    reader = asyncio.create_task(_collect(browser, url))
    await asyncio.sleep(0.3)
    # A zero-second slice budget forces the boundary the design promises, so
    # the subscriber sees a re-queue and a second claim rather than one burst.
    await _drain(engine, max_slice_seconds=0)
    frames = await reader

    assert [frame.event for frame in frames] == [
        "run_created",
        "run_lease_acquired",
        "run_slice_ended",
        "run_lease_acquired",
        "run_completed",
    ]
    assert [frame.id for frame in frames] == ["1", "2", "3", "4", "5"]


async def test_an_idle_stream_sends_a_heartbeat(
    settings: Settings, client: TestClient, scope: dict[str, str], submitted_run: Any
) -> None:
    """A proxy that sees nothing for minutes will close an honest connection."""
    cookies = {name: value for name, value in client.cookies.items()}
    async with _serving(settings.model_copy(update={"sse_heartbeat_seconds": 5})) as url:
        async with httpx.AsyncClient(
            base_url=url, cookies=cookies, timeout=STREAM_TIMEOUT
        ) as subscriber:
            comments: list[bytes] = []
            async with subscriber.stream(
                "GET", _events_url(submitted_run["id"], scope["X-Workspace-Id"], 1)
            ) as response:
                async for chunk in response.aiter_raw():
                    if chunk.startswith(b":"):
                        comments.append(chunk)
                        break

    assert comments != []


async def test_a_disconnected_subscriber_leaves_no_open_transaction(
    browser: httpx.AsyncClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    submitted_run: dict[str, Any],
) -> None:
    url = _events_url(submitted_run["id"], scope["X-Workspace-Id"])

    async with browser.stream("GET", url) as response:
        assert response.status_code == 200
        async for _ in httpx.EventSource(response):
            break  # walk away with the run still queued and the stream open

    await asyncio.sleep(1.0)
    async with engine.connect() as connection:
        idle = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND state = 'idle in transaction'"
                )
            )
        ).scalar_one()
    assert idle == 0
