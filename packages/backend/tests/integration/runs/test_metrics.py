"""What an operator reads off the metrics port (observability design §3.1, §3.3).

The registry is the process's own, shared by every test in the session, so
each assertion compares against the value read before the action.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.model_router import ModelRouter
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import RunSignal
from tiny_hermes.runs.infrastructure.deterministic_model import DeterministicModelProvider
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason
from tiny_hermes.runs.ports.store import ApplySignalCommand
from tiny_hermes.shared.metrics import serve_metrics

from ..support import metric
from .test_scheduler import (
    FULL,
    _abandon_claim,  # pyright: ignore[reportPrivateUsage]
    _expire_lease,  # pyright: ignore[reportPrivateUsage]
    _scheduler,  # pyright: ignore[reportPrivateUsage]
)


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


def _scrape(token: str | None, *, sent: str | None = None) -> tuple[int, str]:
    server, thread = serve_metrics(host="127.0.0.1", port=0, token=token)
    try:
        port = server.server_address[1]
        request = Request(f"http://127.0.0.1:{port}/metrics")  # noqa: S310
        if sent is not None:
            request.add_header("Authorization", f"Bearer {sent}")
        try:
            with urlopen(request, timeout=5) as response:  # noqa: S310
                return response.status, response.read().decode()
        except HTTPError as refused:
            return refused.code, ""
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_the_metrics_port_serves_the_registry() -> None:
    status, body = _scrape(None)

    assert status == 200
    assert "tiny_hermes_claim_wait_seconds" in body
    assert "tiny_hermes_scheduler_scan_seconds" in body


def test_with_a_token_the_metrics_port_answers_only_its_bearer() -> None:
    """Design §4.2: a sandbox can reach every port of the egress proxy."""
    assert _scrape("metrics-token", sent=None)[0] == 401
    assert _scrape("metrics-token", sent="wrong")[0] == 401
    assert _scrape("metrics-token", sent="metrics-token")[0] == 200


def test_the_public_api_port_has_no_metrics(client: TestClient) -> None:
    """§4.1: the API is what the gateway exposes; metrics stay off it."""
    assert client.get("/metrics").status_code == 404


async def test_a_model_call_is_timed_by_provider_and_the_finished_run_counted(
    queued: dict[str, Any], engine: AsyncEngine
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    calls = metric("tiny_hermes_model_call_seconds_count", provider="deterministic")
    completed = metric("tiny_hermes_runs_finished_total", status="completed")

    await WorkerRuntime(
        session_factory=sessions,
        model=ModelRouter(
            deterministic=DeterministicModelProvider(delay_ms=0),
            session_factory=sessions,
            client_factory=_no_network,
        ),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="metrics-router",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    ).run_once()

    assert metric("tiny_hermes_model_call_seconds_count", provider="deterministic") == calls + 1
    assert metric("tiny_hermes_runs_finished_total", status="completed") == completed + 1


def _no_network() -> Any:
    raise AssertionError("a deterministic policy needs no client")


async def test_a_rolled_back_transition_is_not_counted(
    queued: dict[str, Any], scope: dict[str, str], engine: AsyncEngine
) -> None:
    """Counted on commit: a transaction that rolls back finished nothing."""
    cancelled = metric("tiny_hermes_runs_finished_total", status="cancelled")
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        store = SqlRunStore(session)
        await store.apply_signal(
            ApplySignalCommand(
                workspace_id=UUID(scope["X-Workspace-Id"]),
                run_id=UUID(str(queued["id"])),
                signal=RunSignal.CANCEL_REQUESTED,
                pause_reason=None,
                request_id="metrics-rollback",
                capabilities=FULL,
            )
        )
        await session.rollback()

    assert metric("tiny_hermes_runs_finished_total", status="cancelled") == cancelled


async def test_a_recovery_is_counted_by_outcome(
    queued: dict[str, Any], engine: AsyncEngine
) -> None:
    approved = metric("tiny_hermes_recoveries_total", outcome="approved")
    claimed = await _abandon_claim(engine)
    await _expire_lease(engine, claimed.run.id)

    await _scheduler(engine).run_once()

    assert metric("tiny_hermes_recoveries_total", outcome="approved") == approved + 1


def test_a_request_is_timed_by_its_route_not_its_path(
    client: TestClient, scope: dict[str, str], queued: dict[str, Any]
) -> None:
    """A Run id in the label would be a new series per Run (design §2)."""
    route = "/api/v1/runs/{run_id}"
    before = metric("tiny_hermes_http_request_seconds_count", route=route, status_class="2xx")

    assert client.get(f"/api/v1/runs/{queued['id']}", headers=scope).status_code == 200

    assert (
        metric("tiny_hermes_http_request_seconds_count", route=route, status_class="2xx")
        == before + 1
    )
    by_path = f"/api/v1/runs/{queued['id']}"
    assert metric("tiny_hermes_http_request_seconds_count", route=by_path) == 0
