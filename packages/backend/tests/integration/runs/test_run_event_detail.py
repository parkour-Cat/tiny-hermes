"""What a round and each tool call leave on the Run's timeline (§11.6).

The design asks RunEvents to record "工具调用、实际参数摘要、结果和耗时" and
"Token、预计费用和延迟". Until now a tool call lived only in the transcript and
a round's usage only in the last round's checkpoint, so a person watching the
event stream could not tell what a round called, how long it took, or what it
cost.

The argument summary is keys and sizes, never values: §19's acceptance item 7
requires secret values to be stopped on every RunEvent serialization path, and
a summary that holds no value has nothing to leak.
"""

import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelResponse, StopReason, UsageQuality

from ..conftest import VALID_SPEC
from .test_worker_tools import StandInSandbox, drive, submit


@pytest.fixture
def agent(client: TestClient, scope: dict[str, str]) -> str:
    created = client.post(
        "/api/v1/agents",
        headers=scope,
        json={"name": "Watched", "alias": f"watched-{uuid4().hex[:8]}"},
    ).json()
    draft = client.put(
        f"/api/v1/agents/{created['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": ["shell.exec"]}},
    ).json()
    published = client.post(
        f"/api/v1/agents/{created['id']}/publish",
        headers=scope,
        json={"expected_revision": draft["revision"]},
    )
    assert published.status_code == 201, published.text
    return str(created["id"])


SECRET = "sk-live-4f7c9e1b2a"  # noqa: S105 - a fake token the test proves never leaks


class Scripted:
    def __init__(self, *answers: ModelResponse) -> None:
        self._answers = list(answers)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        return self._answers.pop(0) if self._answers else ModelResponse(
            stop_reason=StopReason.COMPLETED, text="done", input_tokens=5, output_tokens=2
        )


def _calls(*calls: ToolCallBlock) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=calls,
        input_tokens=120,
        output_tokens=30,
        usage_quality=UsageQuality.PROVIDER,
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


async def test_each_round_records_what_it_cost_and_how_long_it_took(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: str
) -> None:
    run = submit(client, scope, agent, "list the files")
    model = Scripted(
        _calls(ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "ls"}))
    )

    await drive(engine, model, StandInSandbox())

    rounds = await _events(engine, run, "model_round")
    assert [item["round"] for item in rounds] == [1, 2]
    first = rounds[0]
    assert first["model_calls"] == 1
    assert first["input_tokens"] == 120
    assert first["output_tokens"] == 30
    assert first["usage_quality"] == "provider"
    assert first["stop_reason"] == "tool_call"
    assert isinstance(first["latency_ms"], int) and first["latency_ms"] >= 0


async def test_each_tool_call_records_its_outcome_and_duration(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: str
) -> None:
    run = submit(client, scope, agent, "look around")
    model = Scripted(
        _calls(
            ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "ls -la"}),
            ToolCallBlock(call_id="c2", name="file.write", arguments={"path": "x"}),
        )
    )

    await drive(engine, model, StandInSandbox())

    called = {item["call_id"]: item for item in await _events(engine, run, "tool_called")}
    assert called["c1"]["tool"] == "shell.exec"
    assert called["c1"]["outcome"] == "ok"
    assert called["c1"]["exit_code"] == 0
    assert called["c1"]["output_chars"] == len("a.txt\n")
    assert called["c1"]["arguments"] == {"command": "str:6"}
    assert isinstance(called["c1"]["duration_ms"], int)
    # Not bound on this Agent: refused, and recorded as refused.
    assert called["c2"]["outcome"] == "refused"


async def test_an_argument_value_never_reaches_the_timeline(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: str
) -> None:
    run = submit(client, scope, agent, "call the api")
    model = Scripted(
        _calls(
            ToolCallBlock(
                call_id="c1",
                name="shell.exec",
                arguments={"command": f"curl -H 'Authorization: Bearer {SECRET}' x"},
            )
        )
    )

    await drive(engine, model, StandInSandbox())

    async with engine.connect() as connection:
        payloads = (
            await connection.execute(
                text("SELECT payload FROM run_events WHERE run_id = :id"), {"id": run}
            )
        ).scalars().all()
    assert SECRET not in json.dumps(payloads)
