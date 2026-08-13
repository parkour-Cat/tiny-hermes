import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, cast

import httpx2 as httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tiny_hermes.api.app import create_app
from tiny_hermes.sandbox.infrastructure import tables as sandbox_tables
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.database import Base

from integration.support import EventsUrl, ReadStream

# Imported for the side effect of registering its tables on `Base.metadata`,
# which is what makes the derived TRUNCATE complete. `create_app` reaches every
# other module; the sandbox tables have no route yet and would otherwise be
# absent from the metadata and therefore never truncated.
assert sandbox_tables.SandboxReservationRow.__tablename__

STREAM_TIMEOUT = 20

def _truncate_every_table() -> str:
    """Derived from the metadata rather than listed by hand.

    This was a hand-written list, and the phase-3B sandbox tables were added
    without it. The symptom was not "the new tables are dirty" — it was an
    unrelated test failing on a row some earlier test had left behind, which is
    a bad afternoon for whoever meets it next. Deriving it means a new table
    cannot be forgotten. CASCADE handles the ordering.
    """
    names = ", ".join(sorted(table.name for table in Base.metadata.sorted_tables))
    return f"TRUNCATE {names} CASCADE"


TRUNCATE = _truncate_every_table()

VALID_SPEC: dict[str, Any] = {
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
}

BOOTSTRAP_TOKEN = "a" * 32
PASSWORD = "long-pass-123"  # noqa: S105 - fixed local test credential


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://tiny_hermes:local-only@localhost:54320/tiny_hermes_test",
    )


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(database_url)
    yield value
    await value.dispose()


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Where the wake-up channel lives, if it lives anywhere.

    CI points this at an unused port for one run of the whole suite, because
    Redis is a latency optimization and the platform has to keep working
    without it. Tests that assert the optimization itself skip there; nothing
    else may notice.
    """
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def settings(database_url: str, redis_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        redis_url=redis_url,
        s3_endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        s3_bucket=os.environ.get("S3_BUCKET", "tiny-hermes-test"),
        s3_access_key=os.environ.get("S3_ACCESS_KEY", "tiny-hermes-local"),
        s3_secret_key=os.environ.get("S3_SECRET_KEY", "tiny-hermes-local-password"),
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token=BOOTSTRAP_TOKEN,
    )


@pytest.fixture
async def empty_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(TRUNCATE))


@pytest.fixture
async def client(
    empty_database: None, settings: Settings
) -> AsyncIterator[TestClient]:
    del empty_database
    with TestClient(create_app(settings=settings)) as value:
        yield value


@pytest.fixture
def admin_csrf(client: TestClient) -> str:
    """Bootstrap the platform administrator and return their CSRF token."""
    created = client.post(
        "/api/v1/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={
            "subject": "admin@example.com",
            "display_name": "Admin",
            "password": PASSWORD,
        },
    )
    assert created.status_code == 201
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "admin@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201
    return login.cookies["tiny_hermes_csrf"]


@pytest.fixture
def workspace_id(client: TestClient, admin_csrf: str) -> str:
    created = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Primary"},
    )
    assert created.status_code == 201
    return str(created.json()["id"])


@pytest.fixture
def scope(workspace_id: str, admin_csrf: str) -> dict[str, str]:
    """Headers that select a workspace and authorize a write."""
    return {"X-Workspace-Id": workspace_id, "X-CSRF-Token": admin_csrf}


@pytest.fixture
def published_agent(client: TestClient, scope: dict[str, str]) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Analyst", "alias": "analyst"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201
    return agent_id


@pytest.fixture
def agent_with_scenario(
    client: TestClient, scope: dict[str, str]
) -> Callable[..., str]:
    """Publish an Agent whose validated policy selects a model scenario."""

    def publish(scenario: str, alias: str = "runner") -> str:
        agent_id = str(
            client.post(
                "/api/v1/agents",
                headers=scope,
                json={"name": alias.title(), "alias": alias},
            ).json()["id"]
        )
        spec = {
            **VALID_SPEC,
            "model_policy": {"provider": "deterministic", "scenario": scenario},
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

    return publish


@pytest.fixture
def session_for(client: TestClient, scope: dict[str, str]) -> Callable[[str], str]:
    def create(agent_id: str) -> str:
        created = client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": agent_id}
        )
        assert created.status_code == 201
        return str(created.json()["id"])

    return create


@pytest.fixture
def submit_run(
    client: TestClient, scope: dict[str, str]
) -> Callable[[str, str], dict[str, Any]]:
    def submit(session_id: str, key: str) -> dict[str, Any]:
        created = client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": key},
            json={"session_id": session_id, "input": f"message {key}"},
        )
        assert created.status_code == 201
        return dict(created.json())

    return submit


@pytest.fixture
def submitted_run(
    client: TestClient, scope: dict[str, str], session_id: str
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "run-1"},
        json={"session_id": session_id, "input": "do the thing"},
    )
    assert created.status_code == 201
    return dict(created.json())


@pytest.fixture
async def concurrent_client(
    settings: Settings, client: TestClient, scope: dict[str, str]
) -> AsyncIterator[httpx.AsyncClient]:
    """A second ASGI client that can issue genuinely parallel requests.

    ``TestClient`` is synchronous, so overlapping requests need an async client
    sharing the already authenticated browser session.
    """
    del scope
    transport = httpx.ASGITransport(app=create_app(settings=settings))
    cookies = {name: value for name, value in client.cookies.items()}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=cookies
    ) as value:
        yield value


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
def server_settings(settings: Settings, request: pytest.FixtureRequest) -> Settings:
    """Settings for the live server, overridable per test.

    A test that needs a different configuration marks itself with
    ``@pytest.mark.settings(field=value)`` rather than standing up its own server.
    """
    # ``FixtureRequest.node`` carries no annotation upstream, so its type has to
    # be asserted here rather than inferred.
    node = cast(pytest.Item, request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("settings")
    if marker is None:
        return settings
    return settings.model_copy(update=dict(marker.kwargs))


@pytest.fixture
async def live_server(
    server_settings: Settings, client: TestClient
) -> AsyncIterator[str]:
    del client  # the bootstrap fixtures must prepare the same database first
    async with _serving(server_settings) as url:
        yield url


@pytest.fixture
async def browser(
    live_server: str, client: TestClient, scope: dict[str, str]
) -> AsyncIterator[httpx.AsyncClient]:
    """A subscriber carrying the already authenticated browser session."""
    del scope  # ordering only: the login must happen before the cookies are read
    cookies = {name: value for name, value in client.cookies.items()}
    async with httpx.AsyncClient(
        base_url=live_server,
        cookies=cookies,
        timeout=STREAM_TIMEOUT,
        trust_env=False,
    ) as value:
        yield value


@pytest.fixture
def events_url(scope: dict[str, str]) -> EventsUrl:
    """Address a Run's event stream the way a browser EventSource must."""

    def build(run_id: Any, cursor: int | None = None, workspace_id: str = "") -> str:
        selected = workspace_id or scope["X-Workspace-Id"]
        url = f"/api/v1/runs/{run_id}/events?workspace_id={selected}"
        return url if cursor is None else f"{url}&last_event_id={cursor}"

    return build


@pytest.fixture
def read_stream(browser: httpx.AsyncClient) -> ReadStream:
    """Read one event stream to its end, as a browser EventSource would."""

    async def collect(
        url: str, headers: dict[str, str] | None = None
    ) -> list[httpx.ServerSentEvent]:
        async def read() -> list[httpx.ServerSentEvent]:
            frames: list[httpx.ServerSentEvent] = []
            async with browser.stream("GET", url, headers=headers or {}) as response:
                assert response.status_code == 200
                async for frame in httpx.EventSource(response):
                    frames.append(frame)
            return frames

        return await asyncio.wait_for(read(), timeout=STREAM_TIMEOUT)

    return collect


@pytest.fixture
def session_id(client: TestClient, scope: dict[str, str], published_agent: str) -> str:
    created = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
    )
    assert created.status_code == 201
    return str(created.json()["id"])
