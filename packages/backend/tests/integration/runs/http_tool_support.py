"""A stand-in API and the scaffolding two suites need to reach it.

`test_http_tool_calls` proves a bound operation reaches somebody else's server;
`test_approvals` proves a write stops before it does. They are two halves of one
path, so the registration, the publication and the server itself live here
rather than being copied — a second copy is a second place for them to drift.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.http_tool_sender import OutboundHttpToolSender
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_approvals import SqlApprovalGate
from tiny_hermes.runs.ports.http_calls import EgressClaim

from ..egress_support import PROXY_TOKEN, ProxyHandle

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
async def serving(app: StandIn) -> AsyncGenerator[str]:
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


def worker(engine: AsyncEngine, workspace_id: str, proxy: ProxyHandle) -> WorkerRuntime:
    """A Worker with the two things an HTTP tool call needs and nothing else.

    The approval gate is real. A Worker built without one refuses every write,
    which is a different behaviour and belongs in its own test rather than
    being the accidental default of every test here.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        approvals=SqlApprovalGate(sessions),
        http_sender=OutboundHttpToolSender(
            sessions,
            lambda claim: SafeOutboundClient(
                egress=route(proxy, claim), connect_timeout=5, read_timeout=10
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


def route(proxy: ProxyHandle, claim: EgressClaim) -> EgressRoute:
    return EgressRoute(
        url=proxy.url,
        token=PROXY_TOKEN,
        workspace_id=claim.workspace_id,
        agent_version_id=claim.agent_version_id,
        run_id=claim.run_id,
    )


def approve_host(client: TestClient, scope: dict[str, str], entry: str) -> None:
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


def register_tool(
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


def ask(
    client: TestClient, scope: dict[str, str], session_id: str, asked: str
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"http-{asked or 'default'}"},
        json={"session_id": session_id, "input": asked},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())
