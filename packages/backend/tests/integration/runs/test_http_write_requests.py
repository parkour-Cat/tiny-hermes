"""What a write actually puts on the wire.

`StandIn` used to record a request's method, path and headers and never its
body, so nothing here could see that the sender dropped the body: every
`createOrder` in this suite reached the far end as an empty POST carrying
`Content-Type: application/json`.
"""

import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelResponse, StopReason

from ..egress_support import ProxyHandle
from .http_tool_support import StandIn, approve_host, ask, register_tool, worker
from .test_http_tool_calls import _agent as http_agent  # pyright: ignore[reportPrivateUsage]
from .test_worker_tools import Recording

ORDER = {"item": "apple", "quantity": 2}


def ordering_document() -> str:
    """`createOrder` takes a JSON body, which is the case under test."""
    return json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Orders", "version": "1"},
            "paths": {
                "/orders": {
                    "get": {"operationId": "listOrders", "summary": "List every order."},
                    "post": {
                        "operationId": "createOrder",
                        "summary": "Place one.",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "item": {"type": "string"},
                                            "quantity": {"type": "integer"},
                                        },
                                    }
                                }
                            }
                        },
                    },
                }
            },
        }
    )


def calls(*made: tuple[str, str, dict[str, Any]]) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=tuple(
            ToolCallBlock(call_id=call_id, name=f"http.orders.{operation}", arguments=arguments)
            for call_id, operation, arguments in made
        ),
    )


def ordering_agent(
    client: TestClient, scope: dict[str, str], url: str, operations: list[str]
) -> str:
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url, body=ordering_document())
    return http_agent(client, scope, version_id, operations, ["127.0.0.1"], "preauthorized")


async def test_a_write_sends_its_body(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    stand_in, url = api
    agent_id = ordering_agent(client, scope, url, ["createOrder"])
    ask(client, scope, session_for(agent_id), "order")
    model = Recording(calls(("c1", "createOrder", {"body": ORDER})))

    await worker(engine, scope["X-Workspace-Id"], proxy, model).run_once()

    assert stand_in.methods == ["POST"]
    assert json.loads(stand_in.bodies[0]) == ORDER
    assert stand_in.requests[0][2]["content-type"] == "application/json"
