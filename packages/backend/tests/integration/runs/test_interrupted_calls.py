"""A Worker lost in the middle of a call to somebody else's API.

Until v2.17 such a Run failed: the platform had marked the round's effect
unknown before sending, and recovery refuses an unknown effect. Before that it
was worse, the model was asked again from the same transcript and the write
could go out a second time with different arguments.

Now the platform writes the round down before the call goes out, and recovery
puts it into the transcript with the call answered "result unknown". These
tests kill a real Worker while the stand-in API is holding the request, let a
real Scheduler recover the Run, and read what the next Worker's model is sent.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolResultBlock
from tiny_hermes.runs.ports.model import ModelRequest

from ..egress_support import ProxyHandle
from .http_tool_support import StandIn, ask, worker
from .test_http_write_requests import ORDER, calls, ordering_agent
from .test_scheduler import (
    _expire_lease,  # pyright: ignore[reportPrivateUsage]
    _scheduler,  # pyright: ignore[reportPrivateUsage]
)
from .test_worker_tools import Recording


async def _killed_during_the_first_request(
    engine: AsyncEngine, workspace_id: str, proxy: ProxyHandle, stand_in: StandIn, model: Any
) -> None:
    """Run a slice and cancel it while the far end holds the first request:
    the far end has the request, the Worker never hears back."""
    task = asyncio.create_task(worker(engine, workspace_id, proxy, model).run_once())
    arrived = asyncio.Event()

    async def kill() -> None:
        arrived.set()
        task.cancel()

    stand_in.on_request = kill
    with pytest.raises(asyncio.CancelledError):
        await task
    assert arrived.is_set()
    stand_in.on_request = None


async def _recovered(engine: AsyncEngine, run_id: str) -> str:
    await _expire_lease(engine, UUID(run_id))
    await _scheduler(engine).run_once()
    return await _status(engine, run_id)


async def _status(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(
                text("SELECT status FROM runs WHERE id = :id"), {"id": UUID(run_id)}
            )
        )


def _answers(request: ModelRequest) -> dict[str, ToolResultBlock]:
    return {
        block.call_id: block
        for message in request.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }


def _keys(stand_in: StandIn) -> list[str | None]:
    return [headers.get("idempotency-key") for _, _, headers in stand_in.requests]


@pytest.fixture
def ordering(
    client: TestClient,
    scope: dict[str, str],
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
) -> str:
    """A Run of an Agent that may list and place orders. Returns its id."""
    _, url = api
    agent_id = ordering_agent(client, scope, url, ["listOrders", "createOrder"])
    return str(ask(client, scope, session_for(agent_id), "order")["id"])


async def test_the_run_goes_back_to_the_queue_and_the_model_is_told(
    ordering: str,
    scope: dict[str, str],
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    stand_in, _ = api
    workspace = scope["X-Workspace-Id"]
    first = Recording(calls(("c1", "createOrder", {"body": ORDER})))
    await _killed_during_the_first_request(engine, workspace, proxy, stand_in, first)

    assert await _recovered(engine, ordering) == "queued"
    async with engine.connect() as connection:
        named = await connection.scalar(
            text(
                "SELECT payload->>'interrupted_call_id' FROM run_events "
                "WHERE run_id = :id AND event_type = 'run_recovery_approved'"
            ),
            {"id": UUID(ordering)},
        )
    assert named == "c1"

    second = Recording()
    await worker(engine, workspace, proxy, second).run_once()

    answer = _answers(second.requests[0])["c1"]
    assert "unknown" in answer.output
    # The model was told; the platform did not send it again.
    assert stand_in.methods == ["POST"]
    assert await _status(engine, ordering) == "completed"


async def test_a_call_the_round_never_reached_is_answered_too(
    ordering: str,
    scope: dict[str, str],
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    stand_in, _ = api
    workspace = scope["X-Workspace-Id"]
    first = Recording(
        calls(("c1", "createOrder", {"body": ORDER}), ("c2", "listOrders", {}))
    )
    await _killed_during_the_first_request(engine, workspace, proxy, stand_in, first)
    await _recovered(engine, ordering)

    second = Recording()
    await worker(engine, workspace, proxy, second).run_once()

    answers = _answers(second.requests[0])
    assert "unknown" in answers["c1"].output
    assert "not completed" in answers["c2"].output


async def test_repeating_the_interrupted_write_exactly_sends_the_same_key_once(
    ordering: str,
    scope: dict[str, str],
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """An API that honours `Idempotency-Key` then applies it at most once.
    Used once: a third identical order is a new order."""
    stand_in, _ = api
    workspace = scope["X-Workspace-Id"]
    order = ("createOrder", {"body": ORDER})
    first = Recording(calls(("c1", *order)))
    await _killed_during_the_first_request(engine, workspace, proxy, stand_in, first)
    await _recovered(engine, ordering)

    second = Recording(calls(("c2", *order)), calls(("c3", *order)))
    await worker(engine, workspace, proxy, second).run_once()

    interrupted, repeated, third = _keys(stand_in)
    assert interrupted is not None
    assert repeated == interrupted
    assert third is not None and third != interrupted
    assert [json.loads(body) for body in stand_in.bodies] == [ORDER, ORDER, ORDER]


async def test_a_different_write_after_recovery_gets_its_own_key(
    ordering: str,
    scope: dict[str, str],
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    stand_in, _ = api
    workspace = scope["X-Workspace-Id"]
    first = Recording(calls(("c1", "createOrder", {"body": ORDER})))
    await _killed_during_the_first_request(engine, workspace, proxy, stand_in, first)
    await _recovered(engine, ordering)

    other = {**ORDER, "quantity": 3}
    second = Recording(calls(("c2", "createOrder", {"body": other})))
    await worker(engine, workspace, proxy, second).run_once()

    interrupted, different = _keys(stand_in)
    assert different is not None and different != interrupted


async def test_a_round_that_was_recorded_leaves_nothing_behind(
    ordering: str,
    scope: dict[str, str],
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """Only a round the Worker never got to record is told as interrupted."""
    stand_in, _ = api
    model = Recording(calls(("c1", "createOrder", {"body": ORDER})))

    await worker(engine, scope["X-Workspace-Id"], proxy, model).run_once()

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT inflight_round, checkpoint_effect_status FROM runs "
                    "WHERE id = :id"
                ),
                {"id": UUID(ordering)},
            )
        ).one()
    assert row[0] is None
    assert row[1] == "none"
    assert stand_in.methods == ["POST"]


async def test_nothing_is_told_when_the_workspace_moved_past_the_checkpoint(
    ordering: str,
    scope: dict[str, str],
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The round's own changes may then have been committed, and the account
    would say they were not saved. §12 fails such a Run, and it fails untold."""
    stand_in, _ = api
    workspace = scope["X-Workspace-Id"]
    first = Recording(calls(("c1", "createOrder", {"body": ORDER})))
    await _killed_during_the_first_request(engine, workspace, proxy, stand_in, first)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET checkpoint_workspace_revision_id = gen_random_uuid() "
                "WHERE id = :id"
            ),
            {"id": UUID(ordering)},
        )

    assert await _recovered(engine, ordering) == "failed"

    async with engine.connect() as connection:
        told = await connection.scalar(
            text(
                "SELECT count(*) FROM session_messages "
                "WHERE source_run_id = :id AND role = 'tool'"
            ),
            {"id": UUID(ordering)},
        )
    assert told == 0
