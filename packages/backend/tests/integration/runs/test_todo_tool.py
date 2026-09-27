"""`todo.write` through the Worker (§16.1, v2.12).

The list's only home is the transcript, so the tests read it there — and the
timeline, which says how far along the list is without repeating its words.
"""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason

from ..conftest import VALID_SPEC
from .test_worker_tools import drive, submit, transcript

PLAN = [
    {"id": "1", "content": "read the ticket", "status": "completed"},
    {"id": "2", "content": "patch the handler", "status": "in_progress"},
    {"id": "3", "content": "run the tests", "status": "pending"},
]


def _agent(client: TestClient, scope: dict[str, str], tools: list[str]) -> str:
    created = client.post(
        "/api/v1/agents",
        headers=scope,
        json={"name": "Planner", "alias": f"planner-{uuid4().hex[:8]}"},
    ).json()
    draft = client.put(
        f"/api/v1/agents/{created['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": tools}},
    ).json()
    published = client.post(
        f"/api/v1/agents/{created['id']}/publish",
        headers=scope,
        json={"expected_revision": draft["revision"]},
    )
    assert published.status_code == 201, published.text
    return str(created["id"])


class WritesTheList:
    def __init__(self, items: Any) -> None:
        self._items = items
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(
                stop_reason=StopReason.TOOL_CALL,
                text="",
                tool_calls=(
                    ToolCallBlock(
                        call_id="t1", name="todo.write", arguments={"items": self._items}
                    ),
                ),
                input_tokens=10,
                output_tokens=5,
            )
        return ModelResponse(
            stop_reason=StopReason.COMPLETED, text="done", input_tokens=10, output_tokens=5
        )


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


@pytest.fixture
def planner(client: TestClient, scope: dict[str, str]) -> str:
    return _agent(client, scope, ["todo.write"])


async def test_the_list_comes_back_rendered_and_is_kept_in_the_transcript(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, planner: str
) -> None:
    run = submit(client, scope, planner, "fix the bug")
    model = WritesTheList(PLAN)

    await drive(engine, model, None)

    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "completed"
    said = [content for role, content in await transcript(engine, run) if role == "tool"]
    assert any("[>] 2. patch the handler" in content for content in said)


async def test_writing_the_list_is_not_a_tool_call_against_the_ceiling(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, planner: str
) -> None:
    run = submit(client, scope, planner, "fix the bug")

    await drive(engine, WritesTheList(PLAN), None)

    budget = client.get(f"/api/v1/runs/{run}", headers=scope).json()["budget"]
    assert budget["consumed_tool_calls"] == 0


async def test_the_timeline_counts_the_list_without_its_words(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, planner: str
) -> None:
    run = submit(client, scope, planner, "fix the bug")

    await drive(engine, WritesTheList(PLAN), None)

    assert await _events(engine, run, "todo_updated") == [
        {"total": 3, "completed": 1, "in_progress": 1}
    ]


async def test_a_list_the_platform_cannot_keep_is_answered_with_why(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, planner: str
) -> None:
    run = submit(client, scope, planner, "fix the bug")

    await drive(engine, WritesTheList([{"id": "1", "content": "x", "status": "blocked"}]), None)

    said = [content for role, content in await transcript(engine, run) if role == "tool"]
    assert any("refused: invalid_arguments" in content for content in said)
    assert await _events(engine, run, "todo_updated") == []


async def test_an_agent_that_did_not_bind_it_is_refused(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    agent = _agent(client, scope, ["memory.remember"])
    run = submit(client, scope, agent, "fix the bug")

    await drive(engine, WritesTheList(PLAN), None)

    said = [content for role, content in await transcript(engine, run) if role == "tool"]
    assert any("refused: tool_not_authorized" in content for content in said)
    assert await _events(engine, run, "todo_updated") == []
