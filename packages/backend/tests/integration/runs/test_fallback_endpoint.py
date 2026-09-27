"""A round the main endpoint cannot answer goes to the fallback (§7.4.1, v2.12).

The first two tests run the whole path against two stand-in HTTP servers and
the real `ModelRouter`: which failures count as passing ones is decided by the
provider from an actual status line, not by a response this file made up.

The rest drive the Worker with a stand-in model keyed on which endpoint a
request was routed to — enough to pin the rules that live in the Worker:
the switch lasts for the rest of the Run, the fallback is not sent the main
endpoint's reasoning, and a fallback whose window cannot take the request is
skipped rather than sent something it will refuse.
"""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.agents.domain.models import EndpointModelPolicy
from tiny_hermes.runs.application.model_router import ModelRouter
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import ReasoningBlock, ToolCallBlock
from tiny_hermes.runs.infrastructure.deterministic_model import DeterministicModelProvider
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.openai_model import RetryPolicy
from tiny_hermes.runs.ports.model import (
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)

from ..conftest import VALID_SPEC
from ..egress_support import ProxyHandle
from .test_openai_provider import (
    CREDENTIAL,
    FakeModel,
    endpoint,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    outbound_client,
    proxy,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from .test_worker_tools import StandInSandbox, drive, submit


@pytest.fixture
async def backup(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[FakeModel, str]]:
    """A second stand-in server, the same as `endpoint`."""
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")
    app = FakeModel()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield app, f"http://127.0.0.1:{int(address[1])}/v1"
    finally:
        server.should_exit = True
        await task


def _register(client: TestClient, admin_csrf: str, base_url: str, **fields: Any) -> str:
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "name": f"stand-in-{uuid4().hex[:8]}",
            "kind": "openai_compatible",
            "base_url": base_url,
            "model": "stand-in-large",
            "context_window": 32_768,
            "max_output_tokens": 512,
            "usage_quality": "provider",
            "credential_ref": CREDENTIAL,
            **fields,
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _price(client: TestClient, scope: dict[str, str], endpoint_id: str, i: str, o: str) -> None:
    priced = client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/pricing",
        headers=scope,
        json={"currency": "USD", "input_per_million": i, "output_per_million": o},
    )
    assert priced.status_code in (200, 201), priced.text


def _publish(
    client: TestClient,
    scope: dict[str, str],
    main: str,
    *fallbacks: str,
    tools: list[str] | None = None,
) -> str:
    agent = client.post(
        "/api/v1/agents",
        headers=scope,
        json={"name": "Backed", "alias": f"backed-{uuid4().hex[:8]}"},
    ).json()
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={
            "expected_revision": 1,
            "spec": {
                **VALID_SPEC,
                "tools": tools or [],
                "model_policy": {
                    "provider": "openai_compatible",
                    "endpoint_id": main,
                    "fallback_endpoint_ids": list(fallbacks),
                },
            },
        },
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent['id']}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return str(agent["id"])


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


async def _run_through_the_router(engine: AsyncEngine, egress: ProxyHandle) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await WorkerRuntime(
        session_factory=sessions,
        model=ModelRouter(
            deterministic=DeterministicModelProvider(delay_ms=0),
            session_factory=sessions,
            client_factory=lambda: outbound_client(egress),
            retry=RetryPolicy(max_attempts=1),
        ),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-fallback",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    ).run_once()


async def test_a_main_endpoint_answering_503_hands_the_round_to_the_fallback(
    endpoint: tuple[FakeModel, str],  # noqa: F811
    backup: tuple[FakeModel, str],
    proxy: ProxyHandle,  # noqa: F811
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    engine: AsyncEngine,
) -> None:
    main_app, main_url = endpoint
    main_app.failures = [503] * 10
    backup_app, backup_url = backup
    backup_app.answer = "the backup answered"
    main = _register(client, admin_csrf, main_url)
    spare = _register(client, admin_csrf, backup_url)
    _price(client, scope, main, "3", "15")
    _price(client, scope, spare, "1", "2")
    run = submit(client, scope, _publish(client, scope, main, spare), "say something")

    await _run_through_the_router(engine, proxy)

    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "completed", snapshot
    assert len(backup_app.seen) == 1
    switched = await _events(engine, run, "model_fallback_used")
    assert switched == [{"from": main, "to": spare, "reason": "endpoint_status:503"}]
    [round_event] = await _events(engine, run, "model_round")
    assert round_event["endpoint_id"] == spare
    # Priced at the endpoint that answered: 11 in at 1/M, 7 out at 2/M.
    assert Decimal(snapshot["budget"]["consumed_cost"]) == Decimal("0.000025")


async def test_a_refusal_on_the_merits_is_not_handed_on(
    endpoint: tuple[FakeModel, str],  # noqa: F811
    backup: tuple[FakeModel, str],
    proxy: ProxyHandle,  # noqa: F811
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    engine: AsyncEngine,
) -> None:
    """A 400 says the request is wrong; another vendor would only hide that."""
    main_app, main_url = endpoint
    main_app.failures = [400]
    backup_app, backup_url = backup
    main = _register(client, admin_csrf, main_url)
    spare = _register(client, admin_csrf, backup_url)
    run = submit(client, scope, _publish(client, scope, main, spare), "say something")

    await _run_through_the_router(engine, proxy)

    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "failed"
    assert backup_app.seen == []
    assert await _events(engine, run, "model_fallback_used") == []


# --- Worker rules, with a stand-in model keyed on the routed endpoint ---------


def _unreachable() -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.FAILED,
        text="",
        usage_quality=UsageQuality.UNAVAILABLE,
        failure="endpoint_unreachable",
        transient=True,
    )


class ByEndpoint:
    """Answers per endpoint, in order, and keeps every request each one saw."""

    def __init__(self, answers: dict[str, list[ModelResponse]]) -> None:
        self._answers = answers
        self.seen: dict[str, list[ModelRequest]] = {key: [] for key in answers}

    async def complete(self, request: ModelRequest) -> ModelResponse:
        policy = request.policy
        assert isinstance(policy, EndpointModelPolicy)
        key = str(policy.endpoint_id)
        self.seen.setdefault(key, []).append(request)
        queue = self._answers.get(key, [])
        if queue:
            return queue.pop(0)
        return ModelResponse(
            stop_reason=StopReason.COMPLETED, text="done", input_tokens=5, output_tokens=2
        )


def _tool_call(reasoning: str | None = None) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(ToolCallBlock(call_id=f"c{uuid4().hex[:6]}", name="shell.exec",
                                  arguments={"command": "ls"}),),
        input_tokens=10,
        output_tokens=5,
        reasoning=reasoning,
    )


@pytest.fixture
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")


@pytest.fixture
async def endpoints(
    client: TestClient, admin_csrf: str, credential: None
) -> AsyncIterator[tuple[str, str]]:
    yield (
        _register(client, admin_csrf, "https://models.example.com/v1"),
        _register(client, admin_csrf, "https://backup.example.com/v1"),
    )


async def test_once_switched_the_run_stays_on_the_fallback(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    endpoints: tuple[str, str],
) -> None:
    """A thinking endpoint wants this turn's reasoning back with its tool
    calls, and the fallback's calls carry none — going back would be refused."""
    main, spare = endpoints
    agent = _publish(client, scope, main, spare, tools=["shell.exec"])
    submit(client, scope, agent, "look around")
    model = ByEndpoint({main: [_unreachable()], spare: [_tool_call()]})

    await drive(engine, model, StandInSandbox())

    assert len(model.seen[main]) == 1
    assert len(model.seen[spare]) == 2


async def test_the_fallback_is_not_sent_the_main_endpoint_s_reasoning(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    endpoints: tuple[str, str],
) -> None:
    main, spare = endpoints
    agent = _publish(client, scope, main, spare, tools=["shell.exec"])
    run = submit(client, scope, agent, "look around")
    model = ByEndpoint(
        {main: [_tool_call(reasoning="the main model thinking"), _unreachable()], spare: []}
    )

    await drive(engine, model, StandInSandbox())

    [sent] = model.seen[spare]
    assert not any(
        isinstance(block, ReasoningBlock) for message in sent.messages for block in message.blocks
    )
    # Kept in the record: only the request to the fallback leaves it out.
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT m.content::text FROM session_messages m JOIN runs r "
                    "ON r.session_id = m.session_id WHERE r.id = :id"
                ),
                {"id": run},
            )
        ).scalars().all()
    assert any("the main model thinking" in item for item in stored)


async def test_a_fallback_whose_window_cannot_take_the_request_is_skipped(
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    engine: AsyncEngine,
    endpoints: tuple[str, str],
) -> None:
    main, _ = endpoints
    tiny = _register(
        client,
        admin_csrf,
        "https://tiny.example.com/v1",
        context_window=4_200,
        max_output_tokens=4_096,
    )
    run = submit(client, scope, _publish(client, scope, main, tiny), "say something")
    model = ByEndpoint({main: [_unreachable()], tiny: []})

    await drive(engine, model, None)

    assert model.seen[tiny] == []
    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "failed"
    assert await _events(engine, run, "model_fallback_skipped") == [
        {"endpoint_id": tiny, "reason": "fallback_window_too_small"}
    ]


async def test_the_second_fallback_answers_when_the_first_also_fails(
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    engine: AsyncEngine,
    endpoints: tuple[str, str],
) -> None:
    main, spare = endpoints
    last = _register(client, admin_csrf, "https://last.example.com/v1")
    run = submit(client, scope, _publish(client, scope, main, spare, last), "say something")
    model = ByEndpoint({main: [_unreachable()], spare: [_unreachable()], last: []})

    await drive(engine, model, None)

    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "completed"
    assert [item["to"] for item in await _events(engine, run, "model_fallback_used")] == [
        spare,
        last,
    ]
