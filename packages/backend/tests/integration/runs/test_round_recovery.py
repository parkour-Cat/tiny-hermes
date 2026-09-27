"""A malformed, empty or truncated model reply, recovered within the round.

Product design §7.4.3 (v2.11). Until then each of these was a `failed` Run: a
task a dozen rounds in was lost to one missing bracket, and the model was never
told what it got wrong. Driven through the real Worker entry with a scripted
model, and judged by what persisted — the transcript, the budget, the events —
rather than by what the recovery loop meant to do.
"""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelResponse, StopReason, UsageQuality

from ..conftest import VALID_SPEC
from .test_worker_tools import StandInSandbox, drive, events, submit, transcript


@pytest.fixture
def agent(client: TestClient, scope: dict[str, str]) -> Any:
    def build(*, model_calls: int = 20) -> str:
        alias = f"recover-{uuid4().hex[:8]}"
        created = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Recover", "alias": alias}
        ).json()
        spec = {
            **VALID_SPEC,
            "tools": ["shell.exec"],
            "limits": {**VALID_SPEC["limits"], "max_model_calls": model_calls},
        }
        draft = client.put(
            f"/api/v1/agents/{created['id']}/draft",
            headers=scope,
            json={"expected_revision": 1, "spec": spec},
        ).json()
        published = client.post(
            f"/api/v1/agents/{created['id']}/publish",
            headers=scope,
            json={"expected_revision": draft["revision"]},
        )
        assert published.status_code == 201, published.text
        return str(created["id"])

    return build


def _usage(**fields: Any) -> dict[str, Any]:
    return {
        "input_tokens": 100,
        "output_tokens": 10,
        "usage_quality": UsageQuality.PROVIDER,
        **fields,
    }


def malformed() -> ModelResponse:
    """What `normalize` now returns for arguments that are not JSON: failed,
    with the usage the provider reported for the call."""
    return ModelResponse(
        stop_reason=StopReason.FAILED,
        text="",
        failure="malformed_tool_arguments",
        **_usage(),
    )


def truncated(said: str) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.FAILED,
        text=said,
        failure="max_output_reached",
        continuable=True,
        **_usage(),
    )


def calls_ls() -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(ToolCallBlock(call_id="good", name="shell.exec", arguments={"command": "ls"}),),
        **_usage(),
    )


def answers(said: str) -> ModelResponse:
    return ModelResponse(stop_reason=StopReason.COMPLETED, text=said, **_usage())


class Scripted:
    def __init__(self, *answers_: ModelResponse) -> None:
        self._answers = list(answers_)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        return self._answers.pop(0) if self._answers else answers("done")


def _status(client: TestClient, scope: dict[str, str], run: str) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run}", headers=scope).json())


async def _retries(engine: AsyncEngine, run: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT payload FROM run_events WHERE run_id = :id "
                "AND event_type = 'model_round_retried' ORDER BY sequence"
            ),
            {"id": run},
        )
        return [dict(row[0]) for row in rows.all()]


async def test_a_malformed_tool_call_is_asked_again_and_the_run_goes_on(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: Any
) -> None:
    run = submit(client, scope, agent(), "list the files")
    model = Scripted(malformed(), calls_ls(), answers("two files"))

    await drive(engine, model, StandInSandbox())

    assert _status(client, scope, run)["status"] == "completed"
    # The retry was told why; the note lives only in that one request.
    retry_said = " ".join(message.text for message in model.requests[1].messages)
    assert "valid JSON" in retry_said
    said = " ".join(content for _, content in await transcript(engine, run))
    assert "valid JSON" not in said
    assert '"good"' in said
    assert [event["reason"] for event in await _retries(engine, run)] == [
        "malformed_tool_arguments"
    ]
    # Three model calls, all of them charged: the failed one was real.
    budget = _status(client, scope, run)["budget"]
    assert budget["consumed_model_calls"] == 3
    assert budget["consumed_tokens"] == 3 * 110


async def test_a_reply_that_stays_malformed_fails_the_round_after_two_retries(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: Any
) -> None:
    run = submit(client, scope, agent(), "list the files")
    model = Scripted(malformed(), malformed(), malformed(), calls_ls())

    await drive(engine, model, StandInSandbox())

    body = _status(client, scope, run)
    assert body["status"] == "failed"
    assert body["failure_reason"] == "malformed_tool_arguments"
    assert len(model.requests) == 3


async def test_a_truncated_answer_is_continued_and_stored_whole(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: Any
) -> None:
    run = submit(client, scope, agent(), "write it all")
    model = Scripted(truncated("The first half, "), answers("and the second half."))

    await drive(engine, model, StandInSandbox())

    assert _status(client, scope, run)["status"] == "completed"
    continuation = model.requests[1].messages
    assert continuation[-2].role == "assistant"
    assert continuation[-2].text == "The first half, "
    stored = [content for role, content in await transcript(engine, run) if role == "assistant"]
    assert any("The first half, and the second half." in content for content in stored)
    assert [event["reason"] for event in await _retries(engine, run)] == ["max_output_reached"]


async def test_no_retry_is_made_that_the_call_ceiling_does_not_allow(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: Any
) -> None:
    """§12.4 is not loosened: a retry is a call, prechecked like one."""
    run = submit(client, scope, agent(model_calls=1), "list the files")
    model = Scripted(malformed(), calls_ls())

    await drive(engine, model, StandInSandbox())

    assert len(model.requests) == 1
    assert _status(client, scope, run)["status"] in ("failed", "paused")
    assert "model_round_retried" not in await events(engine, run)
