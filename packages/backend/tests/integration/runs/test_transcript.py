"""What the Agent said, kept where the next round can read it.

`session_messages` has existed since phase 2A and Run creation has always
written the user's message into it. Nothing ever wrote an assistant message,
and nothing noticed, because the deterministic provider branches on a round
counter and never needed to know what the previous round said. A real model
does, so this is where a Session stops being a container for Runs and starts
being a conversation.
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import DeterministicModelProvider
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import (
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)

MESSAGES = text(
    "SELECT role, content, source_run_id, sequence FROM session_messages "
    "WHERE session_id = :session_id ORDER BY sequence"
)


class Recording:
    """A provider that remembers what it was asked, and answers as told."""

    def __init__(self, *answers: ModelResponse) -> None:
        self.seen: list[ModelRequest] = []
        self._answers = list(answers)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.seen.append(request)
        if self._answers:
            return self._answers.pop(0)
        return ModelResponse(stop_reason=StopReason.COMPLETED, text="done")


def _worker(engine: AsyncEngine, model: object) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model,  # pyright: ignore[reportArgumentType]
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-transcript",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    )


async def _messages(engine: AsyncEngine, session_id: str) -> list[Row[Any]]:
    async with engine.connect() as connection:
        found = await connection.execute(MESSAGES, {"session_id": UUID(session_id)})
        return list(found.all())


def _texts(rows: list[Row[Any]]) -> list[tuple[str, str]]:
    return [
        (row.role, "\n".join(part["text"] for part in row.content["parts"]))
        for row in rows
    ]


@pytest.fixture
def session_id(client: TestClient, scope: dict[str, str], published_agent: str) -> str:
    created = client.post(
        "/api/v1/sessions",
        headers=scope,
        json={"agent_id": published_agent, "session_mode": "persistent"},
    )
    assert created.status_code == 201
    return str(created.json()["id"])


def _submit(client: TestClient, scope: dict[str, str], session_id: str, prompt: str) -> str:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"transcript-{prompt}"},
        json={"session_id": session_id, "input": prompt},
    )
    assert created.status_code == 201
    return str(created.json()["id"])


async def test_a_completed_round_leaves_what_the_agent_said(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    _submit(client, scope, session_id, "hello")
    model = Recording(ModelResponse(stop_reason=StopReason.COMPLETED, text="the answer"))
    await _worker(engine, model).run_once()

    assert _texts(await _messages(engine, session_id)) == [
        ("user", "hello"),
        ("assistant", "the answer"),
    ]


async def test_the_assistant_message_names_the_run_that_produced_it(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run_id = _submit(client, scope, session_id, "hello")
    await _worker(engine, Recording()).run_once()
    rows = await _messages(engine, session_id)
    assert [str(row.source_run_id) for row in rows] == [run_id, run_id]
    # Allocated by the Session's own counter, the same mechanism the user
    # message already uses, so no new concurrency reasoning is introduced.
    assert [row.sequence for row in rows] == [1, 2]


async def test_a_failed_round_leaves_nothing(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    """The transcript holds what the Agent said, never what it tried to say."""
    _submit(client, scope, session_id, "hello")
    model = Recording(
        ModelResponse(stop_reason=StopReason.FAILED, text="half an answer before it broke")
    )
    await _worker(engine, model).run_once()

    assert _texts(await _messages(engine, session_id)) == [("user", "hello")]


async def test_two_rounds_leave_two_messages_in_order(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    _submit(client, scope, session_id, "hello")
    model = Recording(
        ModelResponse(stop_reason=StopReason.CONTINUE, text="thinking"),
        ModelResponse(stop_reason=StopReason.COMPLETED, text="the answer"),
    )
    await _worker(engine, model).run_once()

    assert _texts(await _messages(engine, session_id)) == [
        ("user", "hello"),
        ("assistant", "thinking"),
        ("assistant", "the answer"),
    ]


async def test_the_second_round_is_told_what_the_first_one_said(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    """Asserted on the request the provider received, not on the database."""
    _submit(client, scope, session_id, "hello")
    model = Recording(
        ModelResponse(stop_reason=StopReason.CONTINUE, text="thinking"),
        ModelResponse(stop_reason=StopReason.COMPLETED, text="the answer"),
    )
    await _worker(engine, model).run_once()

    assert [(entry.role, entry.text) for entry in model.seen[0].messages] == [
        ("user", "hello")
    ]
    assert [(entry.role, entry.text) for entry in model.seen[1].messages] == [
        ("user", "hello"),
        ("assistant", "thinking"),
    ]


async def test_a_persistent_sessions_second_run_sees_the_first(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    """What `session_mode=persistent` has been promising since phase 2A."""
    _submit(client, scope, session_id, "hello")
    await _worker(engine, Recording(
        ModelResponse(stop_reason=StopReason.COMPLETED, text="the first answer")
    )).run_once()

    _submit(client, scope, session_id, "and again")
    second = Recording()
    await _worker(engine, second).run_once()

    assert [(entry.role, entry.text) for entry in second.seen[0].messages] == [
        ("user", "hello"),
        ("assistant", "the first answer"),
        ("user", "and again"),
    ]


async def test_an_ephemeral_sessions_second_run_does_not(
    client: TestClient, scope: dict[str, str], published_agent: str, engine: AsyncEngine
) -> None:
    """The first behavior `session_mode` has ever had.

    It has been stored since phase 2A and read by nothing. A one-shot Session is
    one-shot precisely in this: a Run inside it is told its own input and
    nothing else, whatever else the Session happens to contain.
    """
    created = client.post(
        "/api/v1/sessions",
        headers=scope,
        json={"agent_id": published_agent, "session_mode": "ephemeral"},
    )
    assert created.status_code == 201
    session_id = str(created.json()["id"])

    _submit(client, scope, session_id, "hello")
    await _worker(engine, Recording(
        ModelResponse(stop_reason=StopReason.COMPLETED, text="the first answer")
    )).run_once()

    _submit(client, scope, session_id, "and again")
    second = Recording()
    await _worker(engine, second).run_once()

    assert [(entry.role, entry.text) for entry in second.seen[0].messages] == [
        ("user", "and again")
    ]


async def test_reported_tokens_reach_the_budget(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run_id = _submit(client, scope, session_id, "hello")
    model = Recording(
        ModelResponse(
            stop_reason=StopReason.COMPLETED,
            text="the answer",
            input_tokens=11,
            output_tokens=7,
        )
    )
    await _worker(engine, model).run_once()

    snapshot = client.get(f"/api/v1/runs/{run_id}", headers=scope).json()
    assert snapshot["budget"]["consumed_tokens"] == 18


async def test_unavailable_usage_fabricates_no_token_count(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    """Nothing is guessed, and the Run still finishes.

    Zero would be a number the platform then has to tell apart from "not
    reported", which is the confusion the nullable counts exist to end.
    """
    run_id = _submit(client, scope, session_id, "hello")
    model = Recording(
        ModelResponse(
            stop_reason=StopReason.COMPLETED,
            text="the answer",
            input_tokens=None,
            output_tokens=None,
            usage_quality=UsageQuality.UNAVAILABLE,
        )
    )
    await _worker(engine, model).run_once()

    snapshot = client.get(f"/api/v1/runs/{run_id}", headers=scope).json()
    assert snapshot["status"] == "completed"
    assert snapshot["budget"]["consumed_tokens"] == 0
    # Recorded rather than merely absent: a reader of the checkpoint can tell
    # "nothing was used" from "nobody counted".
    assert snapshot["checkpoint_usage_quality"] == "unavailable"


async def test_the_deterministic_provider_still_behaves_exactly_as_before(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    """The port moved; the stand-in did not."""
    run_id = _submit(client, scope, session_id, "hello")
    worker = _worker(engine, DeterministicModelProvider(delay_ms=0))
    await worker.run_once()

    snapshot = client.get(f"/api/v1/runs/{run_id}", headers=scope).json()
    assert snapshot["status"] == "completed"
    assert snapshot["budget"]["consumed_tokens"] == 32
