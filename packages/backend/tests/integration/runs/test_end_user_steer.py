"""An end user adds something to a reply that is still being worked on
(§12.1, v2.12 补充).

Not a new Run and not the preemption above it: the Run keeps its goal, its
budget and its rounds, and what the user said reaches the *next* round — at
the boundary, after the previous round's tool results, so a call and its
result are never split. A steer that never got in is handed back, word for
word, rather than dropped.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import TextBlock, ToolCallBlock, ToolResultBlock
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason

from ..conftest import VALID_SPEC
from .test_end_user_run_cancel import (
    ALIAS,
    _cancel,  # pyright: ignore[reportPrivateUsage]
    _sign_in,  # pyright: ignore[reportPrivateUsage]
    _start_session,  # pyright: ignore[reportPrivateUsage]
    _submit_run,  # pyright: ignore[reportPrivateUsage]
    client,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    registered_issuer,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from .test_worker_tools import StandInSandbox, drive

STEER = "use port 8080, not 80"


@pytest.fixture
def steerable_agent(client: TestClient, scope: dict[str, str]) -> None:  # noqa: F811
    spec = {**VALID_SPEC, "tools": ["shell.exec"], "end_user_access": {"enabled": True}}
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Support Bot", "alias": ALIAS}
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
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


def _steer(http: TestClient, run_id: str, said: str = STEER) -> Any:
    return http.post(f"/api/v1/end-user/runs/{run_id}/steer", json={"text": said})


class SteersDuringRoundOne:
    """Round one: the user steers while the model is working, then the model
    asks for a tool (or, with ``finish_first``, says it is done)."""

    def __init__(self, http: TestClient, finish_first: bool = False) -> None:
        self._client = http
        self._finish_first = finish_first
        self.run_id = ""
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            steered = _steer(self._client, self.run_id)
            assert steered.status_code == 202, steered.text
            if self._finish_first:
                return ModelResponse(
                    stop_reason=StopReason.COMPLETED, text="done", input_tokens=5, output_tokens=2
                )
            return ModelResponse(
                stop_reason=StopReason.TOOL_CALL,
                text="",
                tool_calls=(
                    ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "ls"}),
                ),
                input_tokens=5,
                output_tokens=2,
            )
        return ModelResponse(
            stop_reason=StopReason.COMPLETED, text="on 8080", input_tokens=5, output_tokens=2
        )


def _said(request: ModelRequest) -> list[str]:
    """Each message as `role:kind`, with the user's own words for text."""
    shapes: list[str] = []
    for message in request.messages:
        for block in message.blocks:
            if isinstance(block, TextBlock):
                shapes.append(f"{message.role}:{block.text}")
            elif isinstance(block, ToolCallBlock):
                shapes.append(f"{message.role}:call")
            elif isinstance(block, ToolResultBlock):
                shapes.append(f"{message.role}:result")
    return shapes


async def _events(engine: AsyncEngine, run: str, kind: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT payload FROM run_events WHERE run_id = :id AND event_type = :kind "
                "ORDER BY sequence"
            ),
            {"id": run, "kind": kind},
        )
        return [dict(row[0]) for row in rows.all()]


async def test_a_steer_reaches_the_next_round_after_the_tool_result(
    client: TestClient,  # noqa: F811
    engine: AsyncEngine,
    workspace_id: str,
    registered_issuer: None,  # noqa: F811
    steerable_agent: None,
) -> None:
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    model = SteersDuringRoundOne(client)
    model.run_id = _submit_run(client, session_id, "steer-1", "start the service")

    await drive(engine, model, StandInSandbox())

    assert len(model.requests) == 2
    assert _said(model.requests[1])[-4:] == [
        "user:start the service",
        "assistant:call",
        "tool:result",
        f"user:{STEER}",
    ]
    run = client.get(f"/api/v1/end-user/runs/{model.run_id}").json()
    assert run["status"] == "completed"
    assert run["undelivered_steers"] == []
    assert await _events(engine, model.run_id, "run_steered") == [{"count": 1}]
    # Not a Run of its own, and so nothing to preempt with.
    async with engine.connect() as connection:
        runs = (
            await connection.execute(
                text("SELECT count(*) FROM runs WHERE session_id = :id"), {"id": session_id}
            )
        ).scalar_one()
    assert runs == 1


async def test_a_steer_during_the_last_round_keeps_the_run_going(
    client: TestClient,  # noqa: F811
    engine: AsyncEngine,
    workspace_id: str,
    registered_issuer: None,  # noqa: F811
    steerable_agent: None,
) -> None:
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    model = SteersDuringRoundOne(client, finish_first=True)
    model.run_id = _submit_run(client, session_id, "steer-2", "start the service")

    await drive(engine, model, StandInSandbox())

    assert len(model.requests) == 2
    assert _said(model.requests[1])[-1] == f"user:{STEER}"
    assert client.get(f"/api/v1/end-user/runs/{model.run_id}").json()["status"] == "completed"


async def test_a_finished_run_cannot_be_steered(
    client: TestClient,  # noqa: F811
    workspace_id: str,
    registered_issuer: None,  # noqa: F811
    steerable_agent: None,
) -> None:
    _sign_in(client, workspace_id, "zhang")
    run_id = _submit_run(client, _start_session(client), "steer-3")
    assert _cancel(client, run_id, 1).status_code == 200

    refused = _steer(client, run_id)

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "run_not_steerable"


async def test_someone_else_s_run_cannot_be_steered(
    client: TestClient,  # noqa: F811
    workspace_id: str,
    registered_issuer: None,  # noqa: F811
    steerable_agent: None,
) -> None:
    _sign_in(client, workspace_id, "zhang")
    run_id = _submit_run(client, _start_session(client), "steer-4")
    _sign_in(client, workspace_id, "li")

    assert _steer(client, run_id).status_code == 404


async def test_a_steer_that_never_got_in_comes_back_with_the_run(
    client: TestClient,  # noqa: F811
    workspace_id: str,
    registered_issuer: None,  # noqa: F811
    steerable_agent: None,
) -> None:
    _sign_in(client, workspace_id, "zhang")
    run_id = _submit_run(client, _start_session(client), "steer-5")
    assert _steer(client, run_id).status_code == 202
    assert _cancel(client, run_id, 1).status_code == 200

    run = client.get(f"/api/v1/end-user/runs/{run_id}").json()

    assert run["undelivered_steers"] == [STEER]


async def test_an_overlong_steer_is_refused(
    client: TestClient,  # noqa: F811
    workspace_id: str,
    registered_issuer: None,  # noqa: F811
    steerable_agent: None,
) -> None:
    _sign_in(client, workspace_id, "zhang")
    run_id = _submit_run(client, _start_session(client), "steer-6")

    assert _steer(client, run_id, "x" * 4_001).status_code == 422
