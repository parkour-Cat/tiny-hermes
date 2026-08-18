"""Calling somebody else's API, from the catalog to the model and back.

Everything in this file has to be here rather than anywhere cheaper: the claim
is about five components that only meet in a running Run — the outbound scope a
workspace approved, the HTTP tool catalog, the Agent Version that bound one of
its operations, the Worker that answers the call, and the egress proxy the call
has to cross.

The proxy is real. Since M2C-1 nothing in this platform reaches the network
without one, so a test that mocked it would be asserting about a path
production does not take.

Two things are pinned that no unit test can pin.

A **read** goes all the way: the request arrives at the stand-in with the
credential attached, and the answer comes back into the session.

A **write** does not. §16.3 requires a person's approval before an Agent
changes something at an external endpoint, and approvals arrive in the next
step of the plan. The stand-in receives nothing, and there is an event on the
timeline saying why. That test is meant to be replaced, not kept.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.egress.infrastructure.sql_directory import SqlScopeDirectory
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.http_tool_sender import OutboundHttpToolSender
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.http_calls import EgressClaim

from ..conftest import VALID_SPEC
from ..egress_support import PROXY_TOKEN, ProxyHandle, running_proxy

ANSWER = {"orders": [{"id": "a-1", "status": "open"}]}


def document(base_path: str = "/orders") -> str:
    """An export with one read and one write, which is the whole point."""
    return json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Orders", "version": "1"},
            "paths": {
                base_path: {
                    "get": {
                        "operationId": "listOrders",
                        "summary": "List every order.",
                    },
                    "post": {
                        "operationId": "createOrder",
                        "summary": "Place one.",
                        "requestBody": {
                            "content": {
                                "application/json": {"schema": {"type": "object"}}
                            }
                        },
                    },
                }
            },
        }
    )


@dataclass
class StandIn:
    """The API being called, and a record of everything that reached it."""

    requests: list[tuple[str, str, dict[str, str]]] = field(
        default_factory=list[tuple[str, str, dict[str, str]]]
    )

    @property
    def methods(self) -> list[str]:
        return [entry[0] for entry in self.requests]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope["headers"]
        }
        self.requests.append((str(scope["method"]), str(scope["path"]), headers))
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        body = json.dumps(ANSWER).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@contextlib.asynccontextmanager
async def _serving(app: StandIn) -> AsyncGenerator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
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
async def api() -> AsyncIterator[tuple[StandIn, str]]:
    app = StandIn()
    async with _serving(app) as url:
        yield app, url


@pytest.fixture
async def proxy(engine: AsyncEngine) -> AsyncIterator[ProxyHandle]:
    """The real boundary, reading the real layers.

    The SQL directory rather than the in-memory one, because the claim this
    suite makes is precisely that the chain — platform, workspace, and the
    Agent's own `network` out of its published version — is what decides. A
    directory the test filled in by hand would prove none of it.
    """
    async with running_proxy(
        directory=SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))
    ) as handle:
        yield handle


def _worker(
    engine: AsyncEngine, workspace_id: str, proxy: ProxyHandle
) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        http_sender=OutboundHttpToolSender(
            sessions,
            lambda claim: SafeOutboundClient(
                egress=_route(proxy, claim), connect_timeout=5, read_timeout=10
            ),
        ),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _route(proxy: ProxyHandle, claim: EgressClaim) -> EgressRoute:
    return EgressRoute(
        url=proxy.url,
        token=PROXY_TOKEN,
        workspace_id=claim.workspace_id,
        agent_version_id=claim.agent_version_id,
        run_id=claim.run_id,
    )


def _approve(client: TestClient, scope: dict[str, str], entry: str) -> None:
    """The host, at both levels. A workspace may only approve inside the
    platform's, so the platform entry has to exist first."""
    platform = client.post(
        "/api/v1/outbound-scopes/platform",
        headers=scope,
        json={"entry": entry, "note": "the drill's stand-in"},
    )
    assert platform.status_code == 201, platform.text
    workspace = client.post(
        "/api/v1/outbound-scopes/workspace",
        headers=scope,
        json={"entry": entry, "note": "the drill's stand-in"},
    )
    assert workspace.status_code == 201, workspace.text


def _register(
    client: TestClient, scope: dict[str, str], base_url: str, body: str | None = None
) -> str:
    """A tool in the catalog. Returns the id of its first version."""
    created = client.post(
        "/api/v1/http-tools",
        headers=scope,
        json={
            "name": "orders",
            "base_url": base_url,
            "document": body or document(),
            "credential_ref": None,
        },
    )
    assert created.status_code == 201, created.text
    versions = client.get(
        f"/api/v1/http-tools/{created.json()['id']}/versions", headers=scope
    )
    return str(versions.json()[0]["id"])


def _agent(
    client: TestClient,
    scope: dict[str, str],
    version_id: str,
    operations: list[str],
    allow: list[str],
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Caller", "alias": "caller"}
        ).json()["id"]
    )
    spec = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "http_once"},
        "network": {"allow": allow},
        "http_tools": [
            {"http_tool_version_id": version_id, "operations": operations}
        ],
    }
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return agent_id


def _ask(
    client: TestClient, scope: dict[str, str], session_id: str, asked: str
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"http-{asked or 'default'}"},
        json={"session_id": session_id, "input": asked},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _events(engine: AsyncEngine, run_id: Any) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT event_type, payload FROM run_events "
                "WHERE run_id = :id ORDER BY sequence"
            ),
            {"id": UUID(str(run_id))},
        )
        return [{"type": str(row[0]), "payload": row[1]} for row in rows.all()]


def _transcript(client: TestClient, scope: dict[str, str], session_id: str) -> str:
    page = client.get(f"/api/v1/sessions/{session_id}/messages", headers=scope)
    assert page.status_code == 200, page.text
    return page.text


# -- the read path -----------------------------------------------------------


async def test_a_bound_read_reaches_the_api_and_the_answer_reaches_the_session(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    stand_in, url = api
    _approve(client, scope, "127.0.0.1")
    version_id = _register(client, scope, url)
    agent_id = _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    session_id = session_for(agent_id)
    run = _ask(client, scope, session_id, "http.orders.listOrders")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "completed", reloaded
    assert stand_in.methods == ["GET"]
    assert "a-1" in _transcript(client, scope, session_id)


async def test_the_request_went_through_the_proxy_rather_than_straight_out(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The proxy forwards an absolute-URI request and closes the connection
    afterwards, so `Connection: close` on the arriving request is the mark of a
    hop that crossed the boundary."""
    stand_in, url = api
    _approve(client, scope, "127.0.0.1")
    version_id = _register(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    _ask(client, scope, session_id, "http.orders.listOrders")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    _, _, headers = stand_in.requests[0]
    assert headers.get("connection") == "close"
    # Whatever the proxy was authorized with never travels on.
    assert "proxy-authorization" not in headers


# -- the write path, which is the one that does not run yet ------------------


async def test_a_write_is_refused_and_the_api_hears_nothing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§16.3 wants a person's approval before an external write. Until the
    approval path lands, refusing is the only correct behaviour — and the
    stand-in receiving nothing is what says it really is a refusal rather than
    a call whose answer was discarded.

    Replace this test when approvals arrive. Do not delete it before."""
    stand_in, url = api
    _approve(client, scope, "127.0.0.1")
    version_id = _register(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["createOrder"], ["127.0.0.1"])
    )
    run = _ask(client, scope, session_id, "http.orders.createOrder")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.requests == []
    refused = [
        item
        for item in await _events(engine, run["id"])
        if item["type"] == "http_call_refused"
    ]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] == "approval_required"
    assert refused[0]["payload"]["operation"] == "createOrder"


# -- and what the two authorization steps refuse -----------------------------


async def test_an_operation_this_version_did_not_bind_is_refused(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§10.2's second step. The model names an operation the document has and
    the binding does not, and the schema list it was handed is not what
    decides."""
    stand_in, url = api
    _approve(client, scope, "127.0.0.1")
    version_id = _register(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    _ask(client, scope, session_id, "http.orders.createOrder")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.requests == []
    assert "not_authorized" in _transcript(client, scope, session_id)


async def test_a_tool_whose_host_the_workspace_never_approved_cannot_be_registered(
    client: TestClient,
    scope: dict[str, str],
    api: tuple[StandIn, str],
) -> None:
    """Refused at registration rather than at the call: a tool nobody may reach
    would fail in the middle of somebody's Run for a reason unrelated to it."""
    _, url = api

    refused = client.post(
        "/api/v1/http-tools",
        headers=scope,
        json={
            "name": "orders",
            "base_url": url,
            "document": document(),
            "credential_ref": None,
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "host_outside_workspace_scope"


async def test_revoking_the_workspace_entry_stops_the_next_call(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The layer chain, seen from the far side of the boundary.

    The tool registered and the Agent published while the host was approved,
    and both records are immutable. The proxy asks the database on every
    connection, so withdrawing the approval stops the call — which is the whole
    reason the boundary reads the layers rather than being told them.
    """
    stand_in, url = api
    _approve(client, scope, "127.0.0.1")
    version_id = _register(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    run = _ask(client, scope, session_id, "http.orders.listOrders")
    listed = client.get("/api/v1/outbound-scopes/workspace", headers=scope).json()
    revoked = client.delete(f"/api/v1/outbound-scopes/{listed[0]['id']}", headers=scope)
    assert revoked.status_code == 204, revoked.text

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.requests == []
    refused = [
        item
        for item in await _events(engine, run["id"])
        if item["type"] == "http_call_refused"
    ]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] != "approval_required"
