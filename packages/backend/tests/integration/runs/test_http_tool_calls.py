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

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC
from ..egress_support import ProxyHandle
from .http_tool_support import (
    RecordingModel,
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


async def test_a_far_end_that_echoes_the_credential_does_not_hand_it_to_the_model(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23 assertion 7, at the one place a secret can actually come back.

    Secrets are resolved at the moment of an outbound call and injected into
    a header; they never enter the model's context. That is a structural
    guarantee and it is stronger than scrubbing — except for one path, which
    this test is about: the response body. `body = response.content` becomes
    the tool result the model reads, so a far end that reflects the
    `Authorization` header it was sent puts the credential in front of the
    model, into the RunEvent, and within reach of a memory proposal.

    It requires the far end to echo, which is not this platform's doing. But
    the consequence is: the secret moves from "held by the outbound layer"
    into surfaces with entirely different access rules — session content a
    developer may read, and memory that persists.
    """
    stand_in, url = api
    stand_in.echo_header = "authorization"
    monkeypatch.setenv("ECHO_TOOL_TOKEN", "sk-live-do-not-echo-me")
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url, credential_ref="ECHO_TOOL_TOKEN")
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )

    ask(client, scope, session_id, "http.orders.listOrders")
    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    messages = client.get(f"/api/v1/sessions/{session_id}/messages", headers=scope)
    assert messages.status_code == 200, messages.text
    # The far end really did send it back — otherwise this test proves
    # nothing about scrubbing, only that the echo did not happen.
    assert stand_in.requests
    assert "sk-live-do-not-echo-me" not in messages.text


async def test_no_model_request_in_the_run_ever_carries_the_credential(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23 assertion 7's structural half, pinned instead of argued.

    "Secrets never enter the model's context" was, until this test, a
    sentence somebody arrived at by reading the code — which is exactly the
    kind of claim this repository has been wrong about before. The echo path
    was closed by scrubbing the response body; that says nothing about the
    prompt, the tool schemas, the skill lines, or the personality.

    So this records every `ModelRequest` the Worker actually sent during a
    Run that calls a credentialed tool, and looks for the secret in all of
    them at once. `str(request)` rather than a field-by-field walk on
    purpose: a check that named the fields would keep passing the day
    somebody adds a new one.
    """
    stand_in, url = api
    monkeypatch.setenv("PROMPT_TOOL_TOKEN", "sk-never-in-a-prompt")
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url, credential_ref="PROMPT_TOOL_TOKEN")
    session_id = session_for(
        _agent(client, scope, version_id, ["listOrders"], ["127.0.0.1"])
    )
    ask(client, scope, session_id, "http.orders.listOrders")

    model = RecordingModel()
    await worker(engine, scope["X-Workspace-Id"], proxy, model).run_once()

    # The Run really did call the tool, or there was no credential in play
    # and this test would pass without touching the property.
    assert stand_in.requests
    assert model.requests
    for request in model.requests:
        assert "sk-never-in-a-prompt" not in str(request)
