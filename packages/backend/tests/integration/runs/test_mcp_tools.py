"""MCP, from registration to a call and back — and the two ways it stops.

Everything here needs a running Run, a real egress proxy and a real server: the
claim is that §16.2's *two* checks hold across all of them. The first is what
the model is told about; the second is what actually runs, and it is measured
against the revalidated subset rather than against the name the model typed.

Two stopping conditions get their own tests because neither is visible anywhere
cheaper.

**Over budget stops the Run before it spends anything.** The bound subset is
measured against the segment that carries it, and if it does not fit the Run
pauses with `tool_budget_exceeded` — no truncation, and no model call. The
resume test then says the second attempt is measured from the same place: the
first one cost the Run nothing to repeat.

**A server that changed is read, not replayed.** The snapshot fixes which names
may be offered; the server decides what each takes. Growing a schema past the
allowance therefore stops a Run that was fine yesterday, which is exactly the
behaviour that makes revalidation worth doing.
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
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.mcp_gateway import OutboundMcpGateway
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_approvals import SqlApprovalGate
from tiny_hermes.runs.ports.http_calls import EgressClaim
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC
from ..egress_support import PROXY_TOKEN, ProxyHandle
from .http_tool_support import approve_host, ask


@dataclass
class StandInServer:
    """An MCP server this suite can steer.

    `padding` is how the over-budget test is written: a schema grows because a
    server changed it, which is the only way it ever grows in production.
    """

    padding: int = 0
    tools_listed: int = 0
    #: When true, the tool answer carries back the Authorization header it was
    #: sent — a far end that reflects what it received, which is the one way a
    #: credential can travel back into the model's context.
    echo_authorization: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def listing(self) -> dict[str, Any]:
        described: dict[str, Any] = {"type": "string"}
        if self.padding:
            described["description"] = "x" * self.padding
        return {
            "tools": [
                {
                    "name": "search",
                    "description": "Search the index.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": described},
                    },
                },
                {
                    "name": "purge",
                    "description": "Remove everything.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
        }

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        raw = b""
        while True:
            message = await receive()
            raw += message.get("body", b"")
            if not message.get("more_body", False):
                break
        seen = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope["headers"]
        }
        request: dict[str, Any] = json.loads(raw or b"{}")
        method = str(request.get("method", ""))
        if method == "tools/list":
            self.tools_listed += 1
            result: Any = self.listing()
        else:
            params: dict[str, Any] = request.get("params") or {}
            arguments: dict[str, Any] = params.get("arguments") or {}
            self.calls.append((str(params.get("name")), dict(arguments)))
            answered = "two documents"
            if self.echo_authorization:
                answered = f"two documents; you sent {seen.get('authorization', '')}"
            result = {"content": [{"type": "text", "text": answered}]}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
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
async def _serving(app: StandInServer) -> AsyncGenerator[str]:
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
async def mcp_server() -> AsyncIterator[tuple[StandInServer, str]]:
    app = StandInServer()
    async with _serving(app) as url:
        yield app, url


@pytest.fixture(autouse=True)
def api_egress(settings: Settings, proxy: ProxyHandle) -> None:
    """Point the API's own outbound face at the running proxy.

    Registering an MCP server is the first request path that *reads* something
    across the boundary, so unlike every earlier suite this one needs the API
    itself routed. Mutated rather than rebuilt because the proxy's port only
    exists once it is listening, and `outbound_client` reads the settings at
    call time — which is the property that makes this honest rather than a
    trick.
    """
    settings.egress_proxy_url = proxy.url
    settings.egress_proxy_token = PROXY_TOKEN


def _worker(
    engine: AsyncEngine, workspace_id: str, proxy: ProxyHandle
) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        approvals=SqlApprovalGate(sessions),
        mcp=OutboundMcpGateway(
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


async def _post(client: TestClient, path: str, **kwargs: Any) -> Any:
    """A request that makes the API reach outward, without deadlocking.

    `TestClient` blocks the calling thread, and the stand-in server and the
    proxy both live in this test's event loop. A registration issued straight
    from the test body would therefore hold the loop that has to answer it.
    Sent from a worker thread instead, which is also closer to production —
    there, the API and the server are genuinely two processes.
    """
    return await asyncio.to_thread(lambda: client.post(path, **kwargs))


async def _register(
    client: TestClient,
    scope: dict[str, str],
    url: str,
    credential_ref: str | None = None,
) -> str:
    created = await _post(
        client,
        "/api/v1/mcp-servers",
        headers=scope,
        json={"name": "docs", "url": url, "credential_ref": credential_ref},
    )
    assert created.status_code == 201, created.text
    versions = client.get(
        f"/api/v1/mcp-servers/{created.json()['id']}/versions", headers=scope
    )
    return str(versions.json()[0]["id"])


def _agent(
    client: TestClient,
    scope: dict[str, str],
    version_id: str,
    tools: list[str],
    *,
    write_policy: str = "preauthorized",
    budget: dict[str, Any] | None = None,
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Reader", "alias": "reader"}
        ).json()["id"]
    )
    spec: dict[str, Any] = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "mcp_once"},
        "network": {"allow": ["127.0.0.1"]},
        "mcp_tools": [
            {
                "mcp_server_version_id": version_id,
                "tools": tools,
                "write_policy": write_policy,
            }
        ],
    }
    if budget is not None:
        spec["context_budget"] = budget
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


def _status(client: TestClient, scope: dict[str, str], run_id: Any) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run_id}", headers=scope).json())


def _transcript(client: TestClient, scope: dict[str, str], session_id: str) -> str:
    page = client.get(f"/api/v1/sessions/{session_id}/messages", headers=scope)
    assert page.status_code == 200, page.text
    return page.text


# -- registration ------------------------------------------------------------


async def test_registering_reads_the_server_and_records_what_it_offers(
    client: TestClient,
    scope: dict[str, str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    """There is no document to upload — a snapshot is something this platform
    goes and gets, across the boundary like everything else."""
    server, url = mcp_server
    approve_host(client, scope, "127.0.0.1")

    version_id = await _register(client, scope, url)

    assert server.tools_listed == 1
    listed = client.get("/api/v1/mcp-servers", headers=scope).json()
    assert listed[0]["last_validated_at"] is not None
    versions = client.get(
        f"/api/v1/mcp-servers/{listed[0]['id']}/versions", headers=scope
    ).json()
    assert [tool["name"] for tool in versions[0]["tools"]] == ["purge", "search"]
    assert versions[0]["id"] == version_id


async def test_a_host_the_workspace_never_approved_cannot_be_registered(
    client: TestClient,
    scope: dict[str, str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    _, url = mcp_server

    refused = client.post(
        "/api/v1/mcp-servers",
        headers=scope,
        json={"name": "docs", "url": url, "credential_ref": None},
    )

    assert refused.status_code == 422
    assert refused.json()["code"] == "host_outside_workspace_scope"


async def test_refreshing_an_unchanged_server_adds_no_version(
    client: TestClient,
    scope: dict[str, str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    """The point of a version is that somebody reviewed it, and a snapshot
    identical to the last one has nothing new to review."""
    _, url = mcp_server
    approve_host(client, scope, "127.0.0.1")
    await _register(client, scope, url)
    server_id = client.get("/api/v1/mcp-servers", headers=scope).json()[0]["id"]

    again = await _post(client, f"/api/v1/mcp-servers/{server_id}/refresh", headers=scope)

    assert again.status_code == 200
    versions = client.get(
        f"/api/v1/mcp-servers/{server_id}/versions", headers=scope
    ).json()
    assert len(versions) == 1


async def test_a_changed_server_becomes_a_second_version(
    client: TestClient,
    scope: dict[str, str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    server, url = mcp_server
    approve_host(client, scope, "127.0.0.1")
    await _register(client, scope, url)
    server_id = client.get("/api/v1/mcp-servers", headers=scope).json()[0]["id"]
    server.padding = 40

    again = await _post(client, f"/api/v1/mcp-servers/{server_id}/refresh", headers=scope)

    assert again.status_code == 201
    assert again.json()["version_number"] == 2


# -- the two authorization steps ---------------------------------------------


async def test_a_bound_tool_is_offered_called_and_answered(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    server, url = mcp_server
    approve_host(client, scope, "127.0.0.1")
    version_id = await _register(client, scope, url)
    session_id = session_for(_agent(client, scope, version_id, ["search"]))
    run = ask(client, scope, session_id, "mcp.docs.search")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert _status(client, scope, run["id"])["status"] == "completed"
    assert [name for name, _ in server.calls] == ["search"]
    assert "two documents" in _transcript(client, scope, session_id)


async def test_the_subset_is_revalidated_before_the_run_works(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    """§16.2's own words. Once per slice, not once per round — so a remote's
    hiccup cannot make one model call see a different tool list from the next."""
    server, url = mcp_server
    approve_host(client, scope, "127.0.0.1")
    version_id = await _register(client, scope, url)
    listed_at_registration = server.tools_listed
    session_id = session_for(_agent(client, scope, version_id, ["search"]))
    ask(client, scope, session_id, "mcp.docs.search")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    # The Run took two rounds — the call and the answer — and asked once.
    assert server.tools_listed == listed_at_registration + 1


async def test_a_tool_the_version_did_not_bind_is_refused(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    """The server advertises `purge` and this Version did not bind it. §16.2's
    second step is what decides, not what the server offers."""
    server, url = mcp_server
    approve_host(client, scope, "127.0.0.1")
    version_id = await _register(client, scope, url)
    session_id = session_for(_agent(client, scope, version_id, ["search"]))
    ask(client, scope, session_id, "mcp.docs.purge")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert server.calls == []
    assert "not_authorized" in _transcript(client, scope, session_id)


# -- the schema budget -------------------------------------------------------


async def test_a_subset_over_the_budget_pauses_the_run_without_a_model_call(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    """Measured, never truncated. A schema cut down to fit would leave a model
    calling a tool with arguments the far end never agreed to."""
    server, url = mcp_server
    approve_host(client, scope, "127.0.0.1")
    version_id = await _register(client, scope, url)
    session_id = session_for(
        _agent(
            client,
            scope,
            version_id,
            ["search"],
            budget={"segments": [{"segment": "tool_schemas", "max_tokens": 8}]},
        )
    )
    run = ask(client, scope, session_id, "mcp.docs.search")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "tool_budget_exceeded"
    assert server.calls == []
    over = [
        item
        for item in await _events(engine, run["id"])
        if item["type"] == "tool_schema_budget_exceeded"
    ]
    assert len(over) == 1
    assert over[0]["payload"]["estimate"] > over[0]["payload"]["allowance"]
    # No provider call was made, so the round cost nothing.
    assert reloaded["budget"]["consumed_model_calls"] == 0


async def test_resuming_measures_again_and_charges_nothing_for_the_first_try(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
) -> None:
    """The roadmap's exit check. The budget is a size test rather than a
    consumable, so a Run that failed it and was resumed starts from the same
    place — and once the server fits, it runs."""
    server, url = mcp_server
    # Over the segment's 12k ceiling at roughly three characters a token,
    # which is a server that genuinely changed rather than a number this
    # test lowered.
    server.padding = 60_000
    approve_host(client, scope, "127.0.0.1")
    version_id = await _register(client, scope, url)
    session_id = session_for(_agent(client, scope, version_id, ["search"]))
    run = ask(client, scope, session_id, "mcp.docs.search")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()
    assert _status(client, scope, run["id"])["pause_reason"] == "tool_budget_exceeded"

    # The server shrinks back, and a person resumes the Run.
    server.padding = 0
    resumed = client.post(
        f"/api/v1/runs/{run['id']}/resume",
        headers=scope,
        json={"expected_state_version": _status(client, scope, run["id"])["state_version"]},
    )
    assert resumed.status_code == 200, resumed.text
    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "completed"
    assert [name for name, _ in server.calls] == ["search"]
    # Two rounds, both after the resume. The paused attempt charged nothing.
    assert reloaded["budget"]["consumed_model_calls"] == 2


async def test_an_mcp_server_that_echoes_the_credential_does_not_reach_the_model(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    mcp_server: tuple[StandInServer, str],
    proxy: ProxyHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23 assertion 7, on the second path that has it.

    The HTTP tool sender was fixed first; this gateway injects the same
    `Authorization` header and returns `response.content` just as directly,
    so closing one and not the other would be closing half a door. Written
    as its own test rather than folded into the other because the two paths
    share no code — a fix to one says nothing about the other, and a reader
    finding only one test would reasonably assume it did.
    """
    server, url = mcp_server
    server.echo_authorization = True
    monkeypatch.setenv("MCP_ECHO_TOKEN", "sk-mcp-do-not-echo-me")
    approve_host(client, scope, "127.0.0.1")
    version_id = await _register(client, scope, url, credential_ref="MCP_ECHO_TOKEN")
    session_id = session_for(_agent(client, scope, version_id, ["search"]))
    ask(client, scope, session_id, "mcp.docs.search")

    await _worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    transcript = _transcript(client, scope, session_id)
    # The server really did reflect it, or this test proves nothing about
    # scrubbing and only that the echo never happened.
    assert server.calls
    assert "sk-mcp-do-not-echo-me" not in transcript
