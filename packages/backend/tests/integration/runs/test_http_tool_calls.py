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

A **write** does not, unless the Version said what should happen: §16.3's
choice is made at publish, and this suite's Agent chose `disabled`. What a
`governance` write does — stop, ask, resume — lives next door in
`test_approvals`, because it is a claim about a person rather than about a
request.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC
from ..egress_support import ProxyHandle
from .http_tool_support import (
    StandIn,
    approve_host,
    ask,
    document,
    register_tool,
    worker,
)


def _agent(
    client: TestClient,
    scope: dict[str, str],
    version_id: str,
    operations: list[str],
    allow: list[str],
    write_policy: str | None = None,
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
            {
                "http_tool_version_id": version_id,
                "operations": operations,
                "write_policy": write_policy,
            }
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


async def _events(engine: AsyncEngine, run_id: Any) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sql_text(
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
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    agent_id = _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    session_id = session_for(agent_id)
    run = ask(client, scope, session_id, "http.orders.listOrders")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

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
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    ask(client, scope, session_id, "http.orders.listOrders")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    _, _, headers = stand_in.requests[0]
    assert headers.get("connection") == "close"
    # Whatever the proxy was authorized with never travels on.
    assert "proxy-authorization" not in headers


# -- the write path, which is the one that does not run yet ------------------


async def test_a_write_this_version_disabled_is_refused_and_the_api_hears_nothing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§16.3 forces the choice at publish; this Agent chose `disabled`.

    The stand-in receiving nothing is what says it really is a refusal rather
    than a call whose answer was discarded."""
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["createOrder"], ["127.0.0.1"], "disabled")
    )
    run = ask(client, scope, session_id, "http.orders.createOrder")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.requests == []
    refused = [
        item
        for item in await _events(engine, run["id"])
        if item["type"] == "http_call_refused"
    ]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] == "write_disabled"
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
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    ask(client, scope, session_id, "http.orders.createOrder")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

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
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    run = ask(client, scope, session_id, "http.orders.listOrders")
    listed = client.get("/api/v1/outbound-scopes/workspace", headers=scope).json()
    revoked = client.delete(f"/api/v1/outbound-scopes/{listed[0]['id']}", headers=scope)
    assert revoked.status_code == 204, revoked.text

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.requests == []
    refused = [
        item
        for item in await _events(engine, run["id"])
        if item["type"] == "http_call_refused"
    ]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] != "approval_required"
