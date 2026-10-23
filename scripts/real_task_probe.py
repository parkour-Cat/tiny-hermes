"""Long tasks on a real model: how many rounds, where the time goes, what they cost.

Not a gate, like `concurrency_probe.py`, whose stand-in long task follows a fixed
script. A real model decides for itself how many rounds a task takes and what
it runs, so the probe's long-task numbers say nothing about real ones. This one
gives `--tasks` copies of one small coding task to a real endpoint at once and
reads back, per Run, what the model and the sandbox each took.

Usage::

    uv run --no-sync python scripts/real_task_probe.py --endpoint <id> --tasks 5

The task needs only bash, awk and sed (the sandbox image has no Python) and no
network, so it runs on a stack whose sandboxes are offline.

Every task is its own Session, entered as a service account: a background Run,
held only by the workspace's running limit, not the one person's limit the
probe's sign-in would otherwise put every Run under (§1.21.3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess  # noqa: S404 - the probe reads Docker's view of the stack
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from benchmark_m1 import percentile  # noqa: E402
from concurrency_probe import (  # noqa: E402
    API,
    ProbeConsole,
    RunTimes,
    _connect,  # pyright: ignore[reportPrivateUsage]
    _Database,  # pyright: ignore[reportPrivateUsage]
    _docker,  # pyright: ignore[reportPrivateUsage]
    _Sampler,  # pyright: ignore[reportPrivateUsage]
    database_url,
    host_shape,
    worker_containers,
)

TERMINAL = {"completed", "failed", "cancelled", "paused", "waiting_approval"}

#: Small enough to finish in a few minutes, real enough to need a write, a run
#: and usually a fix: the model is not told the answer's shape, only how to
#: check it.
TASK = """\
In the workspace, write a bash script `roman.sh` that converts an integer \
1..3999 given as its first argument to a Roman numeral and prints it, and \
exits 1 with a message for anything else. Then write `test.sh` that checks \
at least 12 cases (including 4, 9, 14, 40, 90, 400, 1994, 3999, 0 and "abc") \
and prints ALL PASS only if every case passes. Run test.sh with bash, fix \
whatever fails, and repeat until it prints ALL PASS. Finish with one line: \
the last line test.sh printed."""

PERSONALITY = (
    "A careful engineer working in a Linux sandbox through shell.exec. "
    "Only bash, coreutils, awk and sed are installed. Keep replies short."
)


class _ProbeConsole(ProbeConsole):
    def limit_model_calls(self, workspace: str, calls: int) -> None:
        self._client.put(
            f"/api/v1/workspaces/{workspace}/limits",
            headers=self._headers(workspace),
            json={"agent_max_model_calls": calls},
        ).raise_for_status()

    def publish_task_agent(self, workspace: str, alias: str, endpoint: str, calls: int) -> str:
        created = self._client.post(
            "/api/v1/agents",
            headers=self._headers(workspace),
            json={"name": alias, "alias": alias},
        )
        created.raise_for_status()
        agent = str(created.json()["id"])
        self._client.put(
            f"/api/v1/agents/{agent}/draft",
            headers=self._headers(workspace),
            json={
                "expected_revision": 1,
                "spec": {
                    "schema_version": 1,
                    "personality": PERSONALITY,
                    "model_policy": {"provider": "openai_compatible", "endpoint_id": endpoint},
                    "tools": ["shell.exec"],
                    "limits": {
                        "max_execution_seconds": 900,
                        "max_elapsed_seconds": 86_400,
                        "max_model_calls": calls,
                        "max_tool_calls": calls * 2,
                        "max_derived_retries": 3,
                    },
                },
            },
        ).raise_for_status()
        self._client.post(
            f"/api/v1/agents/{agent}/publish",
            headers=self._headers(workspace),
            json={"expected_revision": 2},
        ).raise_for_status()
        return agent


class _Postgres:
    """The Postgres container's CPU, sampled with `docker stats` like the Workers'."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.samples: list[float] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        listed = _docker(
            "ps", "--filter", "label=com.docker.compose.service=postgres", "--format", "{{.Names}}"
        ).split()
        self._name = listed[0] if listed else None

    def __enter__(self) -> _Postgres:
        if self._name:
            self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._name:
            self._thread.join(timeout=15)

    def _run(self) -> None:
        if self._name is None:
            return
        while not self._stop.is_set():
            try:
                row = json.loads(
                    _docker("stats", "--no-stream", "--format", "{{json .}}", self._name)
                )
                self.samples.append(float(str(row["CPUPerc"]).rstrip("%") or 0))
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                pass
            self._stop.wait(2.0)


def _events(workspace: str) -> list[dict[str, Any]]:
    async def read() -> list[Any]:
        conn = await _connect(database_url())
        try:
            return list(
                await conn.fetch(
                    "select e.run_id::text as run_id, e.event_type, e.payload::text as payload "
                    "from run_events e join runs r on r.id = e.run_id "
                    "where r.workspace_id = $1::uuid "
                    "and e.event_type in ('model_round', 'tool_called', 'run_failed')",
                    workspace,
                )
            )
        finally:
            await conn.close()

    return [
        {
            "run_id": str(row["run_id"]),
            "type": str(row["event_type"]),
            "payload": json.loads(str(row["payload"])),
        }
        for row in asyncio.run(read())
    ]


def tally(runs: list[RunTimes], events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each Run's rounds, Token and time, split into model, tool and the rest.

    The rest is execution minus model and tool time: the platform's own work,
    getting a sandbox, and a slice given up because none was free. It is the
    only place that waiting shows, so it is a remainder, not a measurement.
    """
    tallied: dict[str, dict[str, Any]] = {
        run.run_id: {
            "status": run.status,
            "execution_s": None
            if run.started_at is None or run.finished_at is None
            else run.finished_at - run.started_at,
            "rounds": 0,
            "model_ms": 0,
            "tool_calls": 0,
            "tool_ms": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "failed_tool_calls": 0,
        }
        for run in runs
    }
    for event in events:
        row = tallied.get(event["run_id"])
        if row is None:
            continue
        payload = event["payload"]
        if event["type"] == "run_failed":
            row["failure_reason"] = payload.get("failure_reason")
            continue
        if event["type"] == "model_round":
            row["rounds"] += 1
            row["model_ms"] += int(payload.get("latency_ms") or 0)
            row["input_tokens"] += int(payload.get("input_tokens") or 0)
            row["output_tokens"] += int(payload.get("output_tokens") or 0)
            row["cached_input_tokens"] += int(payload.get("cached_input_tokens") or 0)
        else:
            row["tool_calls"] += 1
            row["tool_ms"] += int(payload.get("duration_ms") or 0)
            if payload.get("outcome") != "ok" or payload.get("exit_code") not in (0, None):
                row["failed_tool_calls"] += 1
    for row in tallied.values():
        if row["execution_s"] is not None:
            row["other_ms"] = int(row["execution_s"] * 1000) - row["model_ms"] - row["tool_ms"]
    return tallied


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def probe(
    endpoint: str, tasks: int, calls: int, drain_seconds: float, label: str
) -> dict[str, Any]:
    with httpx.Client(base_url=API, timeout=60.0, trust_env=False) as client:
        console = _ProbeConsole(client)
        console.sign_in()
        workspace = console.create_workspace(f"probe-real-{time.time_ns()}")
        console.limit_model_calls(workspace, calls)
        agent = console.publish_task_agent(workspace, f"real-{time.time_ns()}", endpoint, calls)
        sessions = [console.open_session(workspace, agent) for _ in range(tasks)]
        database = _Database(database_url())
        try:
            database.enter_through(sessions, "service_account")
            workers = worker_containers()
            with _Sampler(workspace, workers) as sampler, _Postgres() as postgres:
                started = time.monotonic()
                for index, session in enumerate(sessions):
                    run_id, status, _ = console.create_run(
                        workspace, session, TASK, f"real-{index}-{time.time_ns()}"
                    )
                    if run_id is None:
                        raise SystemExit(f"creating Run {index} answered {status}")
                deadline = started + drain_seconds
                while time.monotonic() < deadline:
                    counts = database.status_counts(workspace)
                    if sum(n for s, n in counts.items() if s in TERMINAL) >= tasks:
                        break
                    time.sleep(2.0)
                wall = time.monotonic() - started
            runs = database.run_times(workspace)
            events = _events(workspace)
        finally:
            database.close()

    per_run = tally(runs, events)

    finished = [r for r in per_run.values() if r["execution_s"] is not None]
    status_counts: dict[str, int] = {}
    for row in per_run.values():
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    tool_calls = [r for r in finished if r["tool_calls"]]
    return {
        "label": label,
        "offered": {"tasks": tasks, "model_calls_allowed": calls, "endpoint": endpoint},
        "stack": {"workers": workers, **host_shape()},
        "wall_s": wall,
        "status_counts": status_counts,
        "execution_s": _summary([r["execution_s"] for r in finished]),
        "rounds": _summary([float(r["rounds"]) for r in finished]),
        "tool_calls": _summary([float(r["tool_calls"]) for r in finished]),
        "model_s": _summary([r["model_ms"] / 1000 for r in finished]),
        "tool_s": _summary([r["tool_ms"] / 1000 for r in finished]),
        "other_s": _summary([r["other_ms"] / 1000 for r in finished]),
        "per_round_other_ms": _summary(
            [r["other_ms"] / r["rounds"] for r in finished if r["rounds"]]
        ),
        "per_tool_call_ms": _summary([r["tool_ms"] / r["tool_calls"] for r in tool_calls]),
        "tokens": {
            "input": sum(r["input_tokens"] for r in per_run.values()),
            "cached_input": sum(r["cached_input_tokens"] for r in per_run.values()),
            "output": sum(r["output_tokens"] for r in per_run.values()),
        },
        # What the first run of this probe had to dig out of the database by
        # hand: every task failed `max_output_reached` on an endpoint capped
        # at 256 output Token, too few to write a script in one call.
        "failure_reasons": dict(
            Counter(str(r["failure_reason"]) for r in per_run.values() if "failure_reason" in r)
        ),
        "failed_tool_calls": sum(r["failed_tool_calls"] for r in per_run.values()),
        "sampled": {
            "max_running": max((s.get("running", 0) for s in sampler.series), default=0),
            "max_db_connections": sampler.max_connections,
            "worker_cpu_percent_max": sampler.worker_cpu_percent,
            "worker_mem_mib_max": sampler.worker_mem_mib,
            "max_sandboxes_at_once": sampler.max_sandboxes_at_once,
            "max_sandboxes_by_state": sampler.max_sandboxes,
            "max_sandbox_mem_mib": sampler.max_sandbox_mem_mib,
            "postgres_cpu_percent": _summary(postgres.samples),
        },
        "runs": per_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--endpoint", required=True, help="the model endpoint id")
    parser.add_argument("--tasks", type=int, default=1, help="tasks started at once")
    parser.add_argument("--model-calls", type=int, default=40, help="each task's model call budget")
    parser.add_argument("--drain-seconds", type=float, default=1200.0)
    parser.add_argument("--label", default="real-task")
    arguments = parser.parse_args()
    result = probe(
        arguments.endpoint,
        arguments.tasks,
        arguments.model_calls,
        arguments.drain_seconds,
        arguments.label,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
