"""What an operator reads off the metrics port (observability design §3.1, §3.3).

The registry is the process's own, shared by every test in the session, so
each assertion compares against the value read before the action.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason
from tiny_hermes.shared.metrics import serve_metrics

from .test_scheduler import _scheduler  # pyright: ignore[reportPrivateUsage]


def _value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class _Peeking:
    """A model that answers once and notes how many lanes were busy then."""

    def __init__(self) -> None:
        self.busy_seen: list[float] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.busy_seen.append(_value("tiny_hermes_worker_lanes", state="busy"))
        return ModelResponse(stop_reason=StopReason.COMPLETED, text="done")


def _worker(engine: AsyncEngine, model: Any) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model,
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="metrics-worker",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    )


@pytest.fixture
def queued(
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    return submit_run(session_for(agent_with_scenario("complete")), "metrics-1")


async def test_a_claimed_run_records_how_long_it_waited(
    queued: dict[str, Any], engine: AsyncEngine
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET queued_at = now() - interval '7 seconds' WHERE id = :id"),
            {"id": queued["id"]},
        )
    count = _value("tiny_hermes_claim_wait_seconds_count")
    total = _value("tiny_hermes_claim_wait_seconds_sum")

    await _worker(engine, _Peeking()).run_once()

    assert _value("tiny_hermes_claim_wait_seconds_count") == count + 1
    assert _value("tiny_hermes_claim_wait_seconds_sum") - total >= 7


async def test_a_lane_is_busy_while_its_slice_runs_and_idle_after(
    queued: dict[str, Any], engine: AsyncEngine
) -> None:
    before = _value("tiny_hermes_worker_lanes", state="busy")
    model = _Peeking()

    await _worker(engine, model).run_once()

    assert model.busy_seen == [before + 1]
    assert _value("tiny_hermes_worker_lanes", state="busy") == before


async def test_a_running_worker_counts_its_lane(engine: AsyncEngine) -> None:
    before = _value("tiny_hermes_worker_lanes", state="total")
    stop = asyncio.Event()
    running = asyncio.create_task(_worker(engine, _Peeking()).run_forever(stop))
    await asyncio.sleep(0.1)

    assert _value("tiny_hermes_worker_lanes", state="total") == before + 1
    stop.set()
    await running
    assert _value("tiny_hermes_worker_lanes", state="total") == before


async def test_every_scan_reports_its_time_and_success(engine: AsyncEngine) -> None:
    started = datetime.now().timestamp()
    runs = _value("tiny_hermes_scheduler_scan_seconds_count", scan="recover_interrupted")

    await _scheduler(engine).run_once()

    assert (
        _value("tiny_hermes_scheduler_scan_seconds_count", scan="recover_interrupted")
        == runs + 1
    )
    for scan in ("reclaim_expired_leases", "expire_approvals", "dispatch_channel_replies"):
        last = _value("tiny_hermes_scheduler_scan_last_success_timestamp", scan=scan)
        assert last >= started, scan


class _ApprovalsBroken(SchedulerRuntime):
    async def _expire_approvals(self, now: datetime) -> None:
        raise RuntimeError("approval scan cannot read this row")


async def test_a_failing_scan_counts_an_error_and_keeps_its_old_success_time(
    engine: AsyncEngine,
) -> None:
    await _scheduler(engine).run_once()
    succeeded = _value("tiny_hermes_scheduler_scan_last_success_timestamp", scan="expire_approvals")
    errors = _value("tiny_hermes_scheduler_scan_errors_total", scan="expire_approvals")
    broken = _ApprovalsBroken(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=168),
    )

    with pytest.raises(ExceptionGroup):
        await broken.run_once()

    assert _value("tiny_hermes_scheduler_scan_errors_total", scan="expire_approvals") == errors + 1
    assert (
        _value("tiny_hermes_scheduler_scan_last_success_timestamp", scan="expire_approvals")
        == succeeded
    )


async def test_the_queue_is_read_from_the_database(
    queued: dict[str, Any], engine: AsyncEngine
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET queued_at = now() - interval '90 seconds' WHERE id = :id"),
            {"id": queued["id"]},
        )
        waiting = await connection.scalar(
            text("SELECT count(*) FROM runs WHERE status = 'queued'")
        )

    await _scheduler(engine).observe_queue()

    assert _value("tiny_hermes_runs_by_status", status="queued") == waiting
    assert _value("tiny_hermes_queue_oldest_age_seconds") >= 90


async def test_an_empty_queue_reports_no_wait(empty_database: None, engine: AsyncEngine) -> None:
    """Nothing queued: the database starts empty."""
    await _scheduler(engine).observe_queue()

    assert _value("tiny_hermes_runs_by_status", status="queued") == 0
    assert _value("tiny_hermes_queue_oldest_age_seconds") == 0


def test_the_metrics_port_serves_the_registry() -> None:
    server, thread = serve_metrics(host="127.0.0.1", port=0)
    try:
        port = server.server_address[1]
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:  # noqa: S310
            body = response.read().decode()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert "tiny_hermes_claim_wait_seconds" in body
    assert "tiny_hermes_scheduler_scan_seconds" in body


def test_the_public_api_port_has_no_metrics(client: TestClient) -> None:
    """§4.1: the API is what the gateway exposes; metrics stay off it."""
    assert client.get("/metrics").status_code == 404
