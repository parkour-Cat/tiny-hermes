"""One Run, one trace, across the database (observability design §5.3).

The API writes the trace context into the Run row; every slice, model call,
tool call and recovery of that Run is a span under it, whichever process and
however much later it runs.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..egress_support import ProxyHandle
from ..tracing_support import named, recorded, trace_id_of
from .http_tool_support import StandIn, ask, worker
from .test_http_write_requests import ORDER, calls, ordering_agent
from .test_scheduler import (
    _abandon_claim,  # pyright: ignore[reportPrivateUsage]
    _expire_lease,  # pyright: ignore[reportPrivateUsage]
    _scheduler,  # pyright: ignore[reportPrivateUsage]
)
from .test_worker_tools import (
    Recording,
    StandInSandbox,
    _text,  # pyright: ignore[reportPrivateUsage]
    _tool,  # pyright: ignore[reportPrivateUsage]
    agent_with_tools,  # noqa: F401  # pyright: ignore[reportUnusedImport] - a fixture
    drive,
    submit,
)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return recorded()


async def _trace_context(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as connection:
        found = await connection.scalar(
            text("SELECT trace_context FROM runs WHERE id = :id"), {"id": UUID(run_id)}
        )
    assert found, "the Run row carries no trace context"
    return str(found)


async def test_the_request_that_created_a_run_is_its_trace(
    spans: InMemorySpanExporter,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
    engine: AsyncEngine,
) -> None:
    run = submit_run(session_for(agent_with_scenario("complete")), "traced-1")

    stored = await _trace_context(engine, str(run["id"]))

    [created] = [
        span for span in named(spans, "POST /api/v1/runs") if span.context is not None
    ]
    assert created.context is not None
    assert trace_id_of(stored) == created.context.trace_id


async def test_a_slice_and_its_model_call_continue_the_run_s_trace(
    spans: InMemorySpanExporter,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_tools: Any,  # noqa: F811
) -> None:
    run = submit(client, scope, agent_with_tools(["shell.exec"]), "list")
    stored = trace_id_of(await _trace_context(engine, run))

    await drive(engine, Recording(_tool("ls"), _text("done")), StandInSandbox())

    [run_slice] = named(spans, "run.slice")
    assert run_slice.context is not None
    assert run_slice.context.trace_id == stored
    assert run_slice.attributes is not None
    assert run_slice.attributes["run.id"] == run
    for child in ("tool.shell", "sandbox.acquire"):
        [span] = named(spans, child)
        assert span.parent is not None and span.parent.span_id == run_slice.context.span_id, child


async def test_a_recovery_is_in_the_run_s_trace(
    spans: InMemorySpanExporter,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
    engine: AsyncEngine,
) -> None:
    run = submit_run(session_for(agent_with_scenario("complete")), "traced-2")
    stored = trace_id_of(await _trace_context(engine, str(run["id"])))
    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)

    await _scheduler(engine).run_once()

    [recovered] = named(spans, "run.recover")
    assert recovered.context is not None and recovered.context.trace_id == stored


async def test_no_trace_context_reaches_somebody_else_s_api(
    spans: InMemorySpanExporter,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """Design §4.3: an internal trace id sent to a third party is a leak."""
    stand_in, url = api
    agent_id = ordering_agent(client, scope, url, ["createOrder"])
    ask(client, scope, session_for(agent_id), "order")
    model = Recording(calls(("c1", "createOrder", {"body": ORDER})))

    with trace.get_tracer("test").start_as_current_span("outer"):
        await worker(engine, scope["X-Workspace-Id"], proxy, model).run_once()

    assert stand_in.methods == ["POST"]
    headers = stand_in.requests[0][2]
    assert "traceparent" not in headers
    assert "tracestate" not in headers
    assert named(spans, "tool.http"), "the call was traced on this side"
