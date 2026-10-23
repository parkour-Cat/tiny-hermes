"""The real-task probe's arithmetic: where each task's time went, from its events."""

from __future__ import annotations

import sys
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
concurrency = _load("concurrency_probe", _SCRIPTS / "concurrency_probe.py")
real = _load("tiny_hermes_real_task_probe", _SCRIPTS / "real_task_probe.py")


def run(run_id: str, status: str, started: float | None, finished: float | None) -> Any:
    return concurrency.RunTimes(
        run_id=run_id, status=status, created_at=0.0, started_at=started, finished_at=finished
    )


def event(run_id: str, kind: str, **payload: Any) -> dict[str, Any]:
    return {"run_id": run_id, "type": kind, "payload": payload}


def test_what_is_neither_model_nor_tool_is_the_rest_of_the_execution() -> None:
    """The waiting the 1024 MB run surfaced (a task queued for a sandbox) is
    only visible as this remainder, so it has to be execution minus both."""
    tallied = real.tally(
        [run("a", "completed", 10.0, 20.0)],
        [
            event("a", "model_round", latency_ms=3000, input_tokens=100, output_tokens=7),
            event("a", "model_round", latency_ms=2000, input_tokens=50, output_tokens=3),
            event("a", "tool_called", duration_ms=400, outcome="ok", exit_code=0),
        ],
    )

    assert tallied["a"]["rounds"] == 2
    assert tallied["a"]["model_ms"] == 5000
    assert tallied["a"]["tool_ms"] == 400
    assert tallied["a"]["other_ms"] == 10_000 - 5000 - 400
    assert tallied["a"]["input_tokens"] == 150


def test_a_failed_task_carries_its_reason() -> None:
    tallied = real.tally(
        [run("a", "failed", 0.0, 4.0)],
        [event("a", "run_failed", failure_reason="max_output_reached")],
    )

    assert tallied["a"]["failure_reason"] == "max_output_reached"
    assert tallied["a"]["rounds"] == 0


def test_a_command_that_exited_non_zero_counts_as_a_failed_tool_call() -> None:
    tallied = real.tally(
        [run("a", "completed", 0.0, 1.0)],
        [
            event("a", "tool_called", duration_ms=10, outcome="ok", exit_code=1),
            event("a", "tool_called", duration_ms=10, outcome="ok", exit_code=0),
        ],
    )

    assert tallied["a"]["failed_tool_calls"] == 1


def test_an_unfinished_task_has_no_remainder() -> None:
    tallied = real.tally([run("a", "running", 0.0, None)], [])

    assert tallied["a"]["execution_s"] is None
    assert "other_ms" not in tallied["a"]


def test_the_summary_percentiles_are_percentiles() -> None:
    """The first write passed 0.5 and 0.95 to a helper that takes 50 and 95,
    and reported something near the minimum as P50 and P95."""
    summary = real._summary([float(n) for n in range(1, 101)])  # pyright: ignore[reportPrivateUsage]

    assert 50 <= summary["p50"] <= 51
    assert 95 <= summary["p95"] <= 96
    assert summary["max"] == 100
