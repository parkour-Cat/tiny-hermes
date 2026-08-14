"""Live §24.1 drivers: the product table, not a skip, and not a lowered gate."""

from __future__ import annotations

import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[5] / "scripts"


def _load(name: str, path: Path) -> Any:
    spec = spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("restart_drill", _SCRIPTS / "restart_drill.py")
_load("workspace_drill", _SCRIPTS / "workspace_drill.py")
benchmark = _load("tiny_hermes_benchmark_m1", _SCRIPTS / "benchmark_m1.py")
live = _load("tiny_hermes_benchmark_live", _SCRIPTS / "benchmark_live.py")


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class MemoryStore:
    def __init__(self, *, events: int = 0, gap_on: int | None = None) -> None:
        self._next = 1
        self._sequences: list[int] = []
        self._types: list[str] = []
        self._writes = 0
        self._gap_on = gap_on
        self.sandbox_row: tuple[str, str, str, str] | None = None
        self.run_state = "queued"
        for _ in range(events):
            self._append("run_created")
        self._writes = 0

    def _append(self, event_type: str, *, allow_gap: bool = False) -> int:
        if allow_gap:
            self._writes += 1
            if self._gap_on is not None and self._writes == self._gap_on:
                self._next += 1
        sequence = self._next
        self._next += 1
        self._sequences.append(sequence)
        self._types.append(event_type)
        return sequence

    def write_one(self, run_id: str, workspace_id: str) -> float:
        self._append("run_created", allow_gap=True)
        return 2.0

    def write_many(self, run_id: str, workspace_id: str, count: int) -> None:
        for _ in range(count):
            self._append("run_created")

    def sequences(self, run_id: str) -> list[int]:
        return list(self._sequences)

    def event_types_after(self, run_id: str, after: int) -> list[str]:
        return [
            name
            for sequence, name in zip(self._sequences, self._types, strict=True)
            if sequence > after
        ]

    def event_count(self) -> int:
        return len(self._sequences)

    def sandbox(self, run_id: str) -> tuple[str, str, str, str] | None:
        return self.sandbox_row

    def run_status(self, run_id: str) -> str | None:
        return self.run_state


class FakeConsole:
    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.runs: list[str] = []
        self.wanted: list[str] = []
        self.statuses: dict[str, str] = {}
        self.agent_scenario = ""
        self.agent_tools: list[str] = []
        self.inputs: list[str] = []
        self.cancelled: list[str] = []
        self._lock = threading.Lock()

    def sign_in(self) -> None:
        return None

    def create_workspace(self, name: str) -> str:
        return "ws-1"

    def publish_agent(
        self,
        workspace: str,
        alias: str,
        scenario: str,
        tools: list[str] | None = None,
    ) -> str:
        self.agent_scenario = scenario
        self.agent_tools = list(tools or [])
        return "agent-1"

    def open_session(self, workspace: str, agent: str) -> str:
        session = f"session-{len(self.sessions)}"
        self.sessions.append(session)
        return session

    def create_run(
        self, workspace: str, session: str, text: str, key: str
    ) -> tuple[str | None, int, float]:
        with self._lock:
            run = f"run-{len(self.runs)}"
            self.runs.append(session)
            self.inputs.append(text)
            self.statuses[run] = "queued"
        return run, 201, 12.0

    def await_status(
        self, workspace: str, run: str, wanted: list[str] | tuple[str, ...], timeout: float
    ) -> tuple[dict[str, Any], float]:
        self.wanted = list(wanted)
        status = "queued" if "queued" in wanted else wanted[0]
        self.statuses[run] = status
        return {"id": run, "status": status, "state_version": 1}, 8.0

    def read(self, workspace: str, run: str) -> dict[str, Any]:
        return {"id": run, "status": self.statuses.get(run, "queued"), "state_version": 1}

    def cookies(self) -> dict[str, str]:
        return {"tiny_hermes_session": "cookie"}

    def csrf(self) -> str:
        return "csrf"

    def cancel(self, workspace: str, run: str) -> None:
        del workspace
        self.cancelled.append(run)


def _harness(
    *,
    console: FakeConsole | None = None,
    store: MemoryStore | None = None,
    digest: str = "sha256:bench",
    clock: FakeClock | None = None,
    compose_calls: list[tuple[str, ...]] | None = None,
) -> Any:
    console = console or FakeConsole()
    store = store or MemoryStore(events=live.PRELOAD_RUN_EVENTS)
    clock = clock or FakeClock()
    compose_calls = compose_calls if compose_calls is not None else []

    @contextmanager
    def open_console() -> Generator[FakeConsole]:
        yield console

    @contextmanager
    def open_store() -> Generator[MemoryStore]:
        yield store

    def compose(*arguments: str) -> None:
        compose_calls.append(arguments)

    def await_healthy(service: str, timeout: float = 90.0) -> None:
        del service, timeout

    def containers() -> set[str]:
        return set()

    def inspect(container_id: str) -> dict[str, str]:
        return {"id": container_id, "paused": "false", "image": "sha256:bench"}

    return live.Harness(
        open_console=open_console,
        open_store=open_store,
        compose=compose,
        await_healthy=await_healthy,
        digest=lambda: digest,
        containers=containers,
        inspect=inspect,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        api="http://127.0.0.1:8000",
        subscribe=None,
    )


def test_planned_seconds_caps_at_the_gate_and_never_extends_it() -> None:
    assert live.planned_seconds(benchmark.CREATE_RUN, None) == 300
    assert live.planned_seconds(benchmark.CREATE_RUN, 10) == 10
    assert live.planned_seconds(benchmark.CREATE_RUN, 10_000) == 300
    assert live.planned_seconds(benchmark.SANDBOX_COLD, 10) is None


def test_a_short_create_run_sample_cannot_pass() -> None:
    result = live.run_driver(
        benchmark.CREATE_RUN,
        1,
        evaluate=benchmark.evaluate,
        sample_type=benchmark.Sample,
        harness=_harness(),
    )
    assert result["status"] == "measured"
    assert result["passed"] is False
    assert any("sampled" in reason for reason in result["reasons"])


def test_create_run_spreads_across_the_gate_sessions() -> None:
    console = FakeConsole()
    live.run_driver(
        benchmark.CREATE_RUN,
        1,
        evaluate=benchmark.evaluate,
        sample_type=benchmark.Sample,
        harness=_harness(console=console),
    )
    assert len(console.sessions) == benchmark.CREATE_RUN.sessions
    assert len(console.runs) == benchmark.CREATE_RUN.rps
    assert set(console.runs) == set(console.sessions[: benchmark.CREATE_RUN.rps])
    assert console.agent_scenario == "complete"
    assert console.agent_tools == []


def test_run_event_fails_when_sequences_have_a_gap() -> None:
    store = MemoryStore(events=live.PRELOAD_RUN_EVENTS, gap_on=3)
    result = live.run_driver(
        benchmark.RUN_EVENT,
        1,
        evaluate=benchmark.evaluate,
        sample_type=benchmark.Sample,
        harness=_harness(store=store),
    )
    assert result["passed"] is False
    assert result["status"] == "measured"
    assert any("lost" in reason or "gap" in reason for reason in result["reasons"])


def test_run_event_preloads_one_hundred_thousand_events() -> None:
    store = MemoryStore(events=0)
    live.run_driver(
        benchmark.RUN_EVENT,
        1,
        evaluate=benchmark.evaluate,
        sample_type=benchmark.Sample,
        harness=_harness(store=store),
    )
    assert store.event_count() >= live.PRELOAD_RUN_EVENTS
    assert live.PRELOAD_RUN_EVENTS == benchmark.PRELOAD_RUN_EVENTS == 100_000


def test_tool_gates_fail_closed_without_an_approved_image() -> None:
    harness = _harness(digest="")
    for gate in (
        benchmark.SANDBOX_COLD,
        benchmark.SANDBOX_WARM,
        benchmark.WORKSPACE_SMALL,
        benchmark.WORKSPACE_LARGE,
        benchmark.NEXT_RUN,
        benchmark.WORKER_RECOVERY,
    ):
        result = live.run_driver(
            gate,
            None,
            evaluate=benchmark.evaluate,
            sample_type=benchmark.Sample,
            harness=harness,
        )
        assert result["passed"] is False
        assert result["status"] == "measured"
        assert any("SANDBOX_IMAGE_DIGEST" in reason for reason in result["reasons"])


def test_worker_recovery_waits_for_queued_not_completed() -> None:
    console = FakeConsole()
    calls: list[tuple[str, ...]] = []
    result = live.run_driver(
        benchmark.WORKER_RECOVERY,
        None,
        evaluate=benchmark.evaluate,
        sample_type=benchmark.Sample,
        harness=_harness(console=console, compose_calls=calls),
    )
    assert "queued" in console.wanted
    assert "completed" not in console.wanted
    assert ("kill", "worker") in calls
    assert ("restart", "scheduler") not in calls
    assert console.cancelled
    assert result["status"] == "measured"
    assert result["passed"] is True


def test_service_recovery_times_health_and_a_new_run() -> None:
    console = FakeConsole()
    calls: list[tuple[str, ...]] = []
    result = live.run_driver(
        benchmark.SERVICE_RECOVERY,
        None,
        evaluate=benchmark.evaluate,
        sample_type=benchmark.Sample,
        harness=_harness(console=console, compose_calls=calls),
    )
    assert ("restart", "api") in calls
    assert console.runs
    assert result["status"] == "measured"
    assert result["passed"] is True


def test_sandbox_warm_fails_when_the_container_id_changes() -> None:
    reasons = live.warm_reasons(
        before_id="abc",
        after_id="def",
        reset_after_first=True,
    )
    assert any("container" in reason for reason in reasons)
    assert any("sandbox_cache_reset" in reason for reason in reasons)
    assert live.warm_reasons("same", "same", False) == []
    full = "6c63cf01565c597b33402b8db515eebda64501b40250b66d6d69f71cf4b2f2d0"
    assert live.same_container("6c63cf01565c", full)
    assert live.warm_reasons(
        "6c63cf01565c",
        "6c63cf01565c597b33402b8db515eebda64501b40250b66d6d69f71cf4b2f2d0",
        False,
    ) == []


def test_sse_reconnect_rejects_a_gap() -> None:
    assert live.reconnect_gap(last_seen=4, first_replayed=6) == 1
    assert live.reconnect_gap(last_seen=4, first_replayed=5) == 0
    assert live.missed_cadence([0.0, 5.0, 10.0], every=5.0, slack=2.0) is False
    assert live.missed_cadence([0.0, 5.0, 20.0], every=5.0, slack=2.0) is True
    assert live.missed_cadence([0.0, 12.0, 17.0, 22.0], every=5.0, slack=2.0) is False


def test_event_writer_uses_the_same_allocator_as_the_store() -> None:
    text = (_SCRIPTS / "benchmark_live.py").read_text(encoding="utf-8")
    assert "next_event_sequence = next_event_sequence +" in text
    assert "RETURNING next_event_sequence" in text
    assert "allow_skip" not in text
    assert "relax" not in text.lower()


def test_run_driver_names_every_section_24_1_gate() -> None:
    assert set(live.DRIVERS) == {gate.name for gate in benchmark.GATES}


def test_drive_gate_loads_benchmark_live() -> None:
    text = (_SCRIPTS / "benchmark_m1.py").read_text(encoding="utf-8")
    assert "benchmark_live.py" in text
    assert "run_driver" in text
    assert callable(benchmark.drive_gate)
