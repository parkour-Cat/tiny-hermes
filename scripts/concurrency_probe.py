"""Measure how many Runs this stack executes at once, and what the queue costs.

Not a gate. §24.1 has no concurrency cell, so this prints numbers and never a
pass. Choosing a threshold is a design change (§24.1's last paragraph), and it
can only be made after numbers like these exist.

Usage::

    DETERMINISTIC_MODEL_DELAY_MS=3000 \\
      docker compose -f deploy/compose/compose.yaml up -d --wait --scale worker=1
    uv run --no-sync python scripts/concurrency_probe.py --rate 1 --seconds 60

Arrivals are open-loop: Runs are submitted on a clock, not when the previous
one finishes, because employees do not wait for each other before typing. A
closed loop would slow its own arrivals down exactly as the stack saturates and
report a queue that never grows.

Every Run goes into its own Session. Runs in one Session are serialized on
purpose (§8.3), so sharing Sessions would measure that FIFO instead of the
Workers.

All times come from the database clock (`created_at`, `started_at`,
`finished_at` on the Run row), so client and server clocks never mix.

The probe only reads the stack's shape. It creates a workspace, an Agent,
Sessions and Runs through the public API, and never scales, restarts or removes
anything: how many Workers to run is the operator's decision, recorded here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess  # noqa: S404 - the probe reads Docker's view of the stack
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from benchmark_live import BenchmarkConsole  # noqa: E402
from benchmark_m1 import percentile  # noqa: E402
from restart_drill import API, COMPOSE_FILE, PROJECT  # noqa: E402

#: The deterministic scenario that answers once and finishes: one model call,
#: no tools, so a Run's service time is the configured delay plus platform
#: overhead and nothing else.
SCENARIO = "complete"
TERMINAL = frozenset({"completed", "failed", "cancelled"})
FAILED = frozenset({"failed", "cancelled"})
DB_SAMPLE_SECONDS = 1.0
DOCKER_SAMPLE_SECONDS = 5.0


def to_mib(usage: str) -> float:
    scales = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "B": 1 / (1024 * 1024)}
    for suffix, scale in scales.items():
        if usage.endswith(suffix):
            return float(usage[: -len(suffix)]) * scale
    return float(usage)


def database_url() -> str:
    # The same variable and default the §24.1 drivers read, so one export
    # points both at the same stack.
    raw = os.environ.get(
        "TINY_HERMES_BENCHMARK_DATABASE",
        "postgresql://tiny_hermes:local-only@127.0.0.1:5432/tiny_hermes",
    )
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _connect(url: str) -> Any:
    import asyncpg  # type: ignore[import-untyped]

    return await asyncpg.connect(url)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class RunTimes:
    run_id: str
    status: str
    created_at: float
    started_at: float | None
    finished_at: float | None


def arrival_offsets(rate: float, seconds: float) -> list[float]:
    if rate <= 0 or seconds <= 0:
        raise ValueError("rate and seconds must both be positive")
    count = int(rate * seconds + 1e-9)
    return [index / rate for index in range(count) if index / rate < seconds]


def peak_overlap(intervals: Sequence[tuple[float, float]]) -> int:
    # Ends sort before starts at the same instant: one Worker finishing a Run
    # and taking the next at t is one Run at a time, not two.
    edges = sorted(
        [(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
        key=lambda edge: (edge[0], edge[1]),
    )
    current = peak = 0
    for _, delta in edges:
        current += delta
        peak = max(peak, current)
    return peak


def _spread(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "p50": percentile(list(values), 50),
        "p95": percentile(list(values), 95),
        "max": max(values),
    }


def summarize(runs: Sequence[RunTimes], *, observed_until: float) -> dict[str, Any]:
    first = min((run.created_at for run in runs), default=observed_until)
    window = max(observed_until - first, 0.0)
    completed = [run for run in runs if run.status == "completed"]
    # A Run still queued when the probe stopped has waited at least this long.
    # Leaving it out would summarize the Runs that got through and hide the
    # ones that did not — the one thing a saturation probe exists to show.
    waits = [
        (run.started_at if run.started_at is not None else observed_until) - run.created_at
        for run in runs
    ]
    busy = [
        (run.started_at, run.finished_at if run.finished_at is not None else observed_until)
        for run in runs
        if run.started_at is not None
    ]
    service = [
        run.finished_at - run.started_at
        for run in completed
        if run.started_at is not None and run.finished_at is not None
    ]
    end_to_end = [
        run.finished_at - run.created_at for run in completed if run.finished_at is not None
    ]
    never_started = sum(1 for run in runs if run.started_at is None)
    return {
        "submitted": len(runs),
        "completed": len(completed),
        "failed": sum(1 for run in runs if run.status in FAILED),
        "unfinished": sum(1 for run in runs if run.finished_at is None),
        "never_started": never_started,
        "status_counts": dict(Counter(run.status for run in runs)),
        "window_s": window,
        "queue_wait_s": _spread(waits),
        "queue_wait_censored": never_started,
        "service_s": _spread(service),
        "end_to_end_s": _spread(end_to_end),
        "throughput_per_s": (len(completed) / window) if window else 0.0,
        "peak_running": peak_overlap(busy),
        "mean_running": (sum(end - start for start, end in busy) / window) if window else 0.0,
    }


LONG_SCENARIO = "long_task"
SANDBOX_LABEL = "tiny-hermes.instance"


class ProbeConsole(BenchmarkConsole):
    """The drill console, plus an Agent whose budget fits the long task.

    A long task makes one model call per round plus the one that finishes. A
    Run whose last allowed call is the one that finishes still ends
    `paused(limit)` (seen 2026-09-29), so the budget leaves one call of room
    rather than merely fitting. The platform caps it (`AGENT_MAX_MODEL_CALLS`,
    20 by default), which is what bounds `--rounds`.
    """

    def publish_long_agent(self, workspace: str, alias: str, rounds: int) -> str:
        created = self._client.post(
            "/api/v1/agents",
            headers=self._headers(workspace),
            json={"name": alias, "alias": alias},
        )
        created.raise_for_status()
        agent = str(created.json()["id"])
        draft = self._client.put(
            f"/api/v1/agents/{agent}/draft",
            headers=self._headers(workspace),
            json={
                "expected_revision": 1,
                "spec": {
                    "schema_version": 1,
                    "personality": "The concurrency probe's long task.",
                    "model_policy": {"provider": "deterministic", "scenario": LONG_SCENARIO},
                    "tools": ["shell.exec"],
                    "limits": {
                        "max_execution_seconds": 900,
                        "max_elapsed_seconds": 86_400,
                        "max_model_calls": rounds + 2,
                        "max_tool_calls": rounds + 2,
                        "max_derived_retries": 3,
                    },
                },
            },
        )
        if draft.status_code == 422 and "round_ceiling_exceeded" in draft.text:
            raise SystemExit(
                f"{rounds} rounds need {rounds + 2} model calls, above this platform's "
                "ceiling: lower --rounds or raise AGENT_MAX_MODEL_CALLS"
            )
        draft.raise_for_status()
        self._client.post(
            f"/api/v1/agents/{agent}/publish",
            headers=self._headers(workspace),
            json={"expected_revision": 2},
        ).raise_for_status()
        return agent


def split_runs(
    runs: Sequence[RunTimes], long_ids: set[str], short_ids: set[str]
) -> tuple[list[RunTimes], list[RunTimes]]:
    return (
        [run for run in runs if run.run_id in long_ids],
        [run for run in runs if run.run_id in short_ids],
    )


def count_states(listed: str) -> dict[str, int]:
    return dict(Counter(line.strip() for line in listed.splitlines() if line.strip()))


class _Database:
    """One connection on its own loop, so each sampling thread has its own."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._loop = asyncio.new_event_loop()
        self._conn: Any = self._loop.run_until_complete(_connect(url))

    def close(self) -> None:
        self._loop.run_until_complete(self._conn.close())
        self._loop.close()

    def _go(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def now(self) -> float:
        return float(self._go(self._conn.fetchval("select extract(epoch from now())")))

    def status_counts(self, workspace: str) -> dict[str, int]:
        rows = self._go(
            self._conn.fetch(
                "select status, count(*) as n from runs where workspace_id = $1::uuid "
                "group by status",
                workspace,
            )
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    def connections(self) -> int:
        return int(
            self._go(
                self._conn.fetchval(
                    "select count(*) from pg_stat_activity where datname = current_database()"
                )
            )
        )

    def run_times(self, workspace: str) -> list[RunTimes]:
        rows = self._go(
            self._conn.fetch(
                "select id::text as id, status, extract(epoch from created_at) as c, "
                "extract(epoch from started_at) as s, extract(epoch from finished_at) as f "
                "from runs where workspace_id = $1::uuid",
                workspace,
            )
        )
        return [
            RunTimes(
                run_id=str(row["id"]),
                status=str(row["status"]),
                created_at=float(row["c"]),
                started_at=None if row["s"] is None else float(row["s"]),
                finished_at=None if row["f"] is None else float(row["f"]),
            )
            for row in rows
        ]


def _docker(*arguments: str) -> str:
    listed = subprocess.run(  # noqa: S603 - every argument is a literal or Docker's own output
        ["docker", *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return listed.stdout


def worker_containers() -> list[str]:
    project = ["-p", PROJECT] if PROJECT else []
    listed = _docker(
        "compose", *project, "-f", COMPOSE_FILE, "ps", "--format", "{{.Service}} {{.Name}}"
    )
    names: list[str] = []
    for row in listed.splitlines():
        service, _, name = row.partition(" ")
        if service == "worker" and name:
            names.append(name.strip())
    return names


def env_int(env: str, wanted: str) -> int | None:
    for line in env.splitlines():
        key, _, value = line.partition("=")
        if key == wanted and value.isdigit():
            return int(value)
    return None


def container_env(container: str) -> str:
    return _docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container)


def host_shape() -> dict[str, Any]:
    info = json.loads(_docker("info", "--format", "{{json .}}"))
    return {"docker_ncpu": info.get("NCPU"), "docker_mem_gib": info.get("MemTotal", 0) / 2**30}


class _Sampler:
    """Queue depth once a second from the database; Worker memory every few seconds."""

    def __init__(self, workspace: str, workers: list[str]) -> None:
        self._workspace = workspace
        self._workers = workers
        self._stop = threading.Event()
        self.series: list[dict[str, Any]] = []
        self.max_connections = 0
        self.worker_mem_mib: dict[str, float] = {}
        self.worker_cpu_percent: dict[str, float] = {}
        #: Sandbox containers at the busiest sample, by state: a frozen sandbox
        #: (between two slices of its Run) is a paused container.
        self.max_sandboxes: dict[str, int] = {}
        self.max_sandbox_mem_mib = 0.0
        self._threads = [
            threading.Thread(target=self._sample_database, daemon=True),
            threading.Thread(target=self._sample_docker, daemon=True),
        ]

    def __enter__(self) -> _Sampler:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=15)

    def _sample_database(self) -> None:
        database = _Database(database_url())
        try:
            while not self._stop.is_set():
                counts = database.status_counts(self._workspace)
                self.series.append({"t": database.now(), **counts})
                self.max_connections = max(self.max_connections, database.connections())
                self._stop.wait(DB_SAMPLE_SECONDS)
        finally:
            database.close()

    def _sample_docker(self) -> None:
        while not self._stop.is_set() and self._workers:
            try:
                listed = _docker("stats", "--no-stream", "--format", "{{json .}}", *self._workers)
            except subprocess.CalledProcessError:
                # A Worker container restarting mid-probe; try again next time.
                listed = ""
            for line in listed.splitlines():
                row = json.loads(line)
                name = str(row["Name"])
                used = to_mib(str(row["MemUsage"]).split("/")[0].strip())
                cpu = float(str(row["CPUPerc"]).rstrip("%") or 0)
                self.worker_mem_mib[name] = max(self.worker_mem_mib.get(name, 0.0), used)
                self.worker_cpu_percent[name] = max(self.worker_cpu_percent.get(name, 0.0), cpu)
            self._sample_sandboxes()
            self._stop.wait(DOCKER_SAMPLE_SECONDS)

    def _sample_sandboxes(self) -> None:
        label = f"label={SANDBOX_LABEL}"
        states = count_states(_docker("ps", "-a", "--filter", label, "--format", "{{.State}}"))
        for state, count in states.items():
            self.max_sandboxes[state] = max(self.max_sandboxes.get(state, 0), count)
        names = _docker("ps", "--filter", label, "--format", "{{.Names}}").split()
        if not names:
            return
        try:
            listed = _docker("stats", "--no-stream", "--format", "{{json .}}", *names)
        except subprocess.CalledProcessError:
            # A sandbox destroyed since `docker ps` listed it: the stack at work.
            # This sample's memory is lost; the next one is taken as usual.
            return
        used = sum(
            to_mib(str(json.loads(line)["MemUsage"]).split("/")[0].strip())
            for line in listed.splitlines()
        )
        self.max_sandbox_mem_mib = max(self.max_sandbox_mem_mib, used)


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def probe(
    rate: float,
    seconds: float,
    drain_seconds: float,
    label: str,
    *,
    long_tasks: int = 0,
    rounds: int = 10,
    lead_seconds: float = 5.0,
) -> dict[str, Any]:
    """Short Runs at `rate`, optionally after `long_tasks` long ones start.

    The long tasks are submitted first and the short Runs `lead_seconds`
    later, so the short ones arrive while long tasks already hold Workers:
    their wait is the fairness a person sending a message would see.
    """
    if rounds < 1:
        raise SystemExit("--rounds must be at least 1")
    offsets = arrival_offsets(rate, seconds)
    workers = worker_containers()
    envs = {name: container_env(name) for name in workers}
    delays = {name: env_int(env, "DETERMINISTIC_MODEL_DELAY_MS") for name, env in envs.items()}
    # Workers per container. The container count alone stopped describing the
    # stack once one container could hold K of them.
    per_container = {name: env_int(env, "WORKER_CONCURRENCY") or 1 for name, env in envs.items()}
    with httpx.Client(base_url=API, timeout=30.0, trust_env=False) as client:
        console = ProbeConsole(client)
        console.sign_in()
        workspace = console.create_workspace(f"probe-{time.time_ns()}")
        agent = console.publish_agent(workspace, f"probe-{time.time_ns()}", SCENARIO)
        _log(f"opening {len(offsets)} sessions (one per Run, outside the timed window)")
        sessions = [console.open_session(workspace, agent) for _ in offsets]
        long_ids: set[str] = set()
        short_ids: set[str] = set()
        long_sessions: list[str] = []
        if long_tasks:
            long_agent = console.publish_long_agent(
                workspace, f"probe-long-{time.time_ns()}", rounds
            )
            long_sessions = [console.open_session(workspace, long_agent) for _ in range(long_tasks)]

        create_ms: list[float] = []
        create_errors = 0
        lock = threading.Lock()

        def submit(index: int) -> None:
            nonlocal create_errors
            try:
                run_id, status, elapsed = console.create_run(
                    workspace, sessions[index], "concurrency probe", f"probe-{label}-{index}"
                )
            except httpx.HTTPError:
                # A refused connection is the API saturating, which is a result.
                with lock:
                    create_errors += 1
                return
            with lock:
                if run_id is None or status >= 400:
                    create_errors += 1
                else:
                    create_ms.append(elapsed)
                    short_ids.add(run_id)

        database = _Database(database_url())
        try:
            with _Sampler(workspace, workers) as sampler:
                for index, session in enumerate(long_sessions):
                    run_id, status, _ = console.create_run(
                        workspace, session, f"rounds={rounds}", f"probe-{label}-long-{index}"
                    )
                    if run_id is None or status >= 400:
                        raise SystemExit(f"long task {index} was refused: HTTP {status}")
                    long_ids.add(run_id)
                if long_sessions:
                    _log(f"started {len(long_sessions)} long tasks of {rounds} rounds")
                    time.sleep(lead_seconds)
                _log(f"submitting {len(offsets)} Runs at {rate}/s over {seconds:.0f}s")
                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=64) as pool:
                    for index, offset in enumerate(offsets):
                        delay = started + offset - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        pool.submit(submit, index)
                _log("draining: waiting for every Run to reach a terminal status")
                deadline = time.monotonic() + drain_seconds
                while time.monotonic() < deadline:
                    counts = database.status_counts(workspace)
                    if sum(counts.values()) >= len(offsets) + len(long_ids) and all(
                        status in TERMINAL for status in counts
                    ):
                        break
                    time.sleep(1.0)
            observed_until = database.now()
            runs = database.run_times(workspace)
        finally:
            database.close()

    summary = summarize(runs, observed_until=observed_until)
    long_runs, short_runs = split_runs(runs, long_ids, short_ids)
    groups = (
        {
            "long": summarize(long_runs, observed_until=observed_until),
            "short": summarize(short_runs, observed_until=observed_until),
        }
        if long_ids
        else {}
    )
    return {
        "label": label,
        "offered": {
            "rate_per_s": rate,
            "seconds": seconds,
            "runs": len(offsets),
            "long_tasks": long_tasks,
            "rounds": rounds if long_tasks else 0,
        },
        "stack": {
            **host_shape(),
            "workers": len(workers),
            "worker_concurrency": sorted(set(per_container.values())),
            "total_workers": sum(per_container.values()),
            "model_delay_ms": sorted({delay for delay in delays.values() if delay is not None}),
        },
        "create_run": {"errors": create_errors, "latency_ms": _spread(create_ms)},
        "summary": summary,
        "groups": groups,
        "sampled": {
            "max_queued": max((row.get("queued", 0) for row in sampler.series), default=0),
            "max_running": max((row.get("running", 0) for row in sampler.series), default=0),
            "max_db_connections": sampler.max_connections,
            "worker_mem_mib_max": sampler.worker_mem_mib,
            "worker_cpu_percent_max": sampler.worker_cpu_percent,
            "max_sandboxes_by_state": sampler.max_sandboxes,
            "max_sandbox_mem_mib": sampler.max_sandbox_mem_mib,
        },
        "series": sampler.series,
    }


def _report(result: dict[str, Any]) -> None:
    summary = result["summary"]
    wait = summary["queue_wait_s"]
    _log(
        f"{result['label']}: containers={result['stack']['workers']} "
        f"workers={result['stack']['total_workers']} "
        f"delay={result['stack']['model_delay_ms']}ms offered={result['offered']['rate_per_s']}/s"
    )
    _log(
        f"  completed {summary['completed']}/{summary['submitted']}, "
        f"failed {summary['failed']}, unfinished {summary['unfinished']}"
    )
    _log(
        f"  queue wait p50 {wait['p50']:.1f}s p95 {wait['p95']:.1f}s max {wait['max']:.1f}s"
        f" (censored {summary['queue_wait_censored']})"
    )
    _log(
        f"  throughput {summary['throughput_per_s']:.2f}/s, "
        f"peak running {summary['peak_running']}, mean running {summary['mean_running']:.1f}"
    )
    for name, group in result["groups"].items():
        group_wait = group["queue_wait_s"]
        _log(
            f"  {name}: {group['completed']}/{group['submitted']} done, "
            f"wait p50 {group_wait['p50']:.1f}s p95 {group_wait['p95']:.1f}s "
            f"max {group_wait['max']:.1f}s, end-to-end p95 {group['end_to_end_s']['p95']:.1f}s"
        )
    if result["sampled"]["max_sandboxes_by_state"]:
        _log(
            f"  sandboxes at most {result['sampled']['max_sandboxes_by_state']}, "
            f"{result['sampled']['max_sandbox_mem_mib']:.0f} MiB"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure how many Runs this stack executes at once. Not a gate."
    )
    parser.add_argument("--rate", type=float, required=True, help="Runs submitted per second")
    parser.add_argument("--seconds", type=float, default=60.0, help="submission window")
    parser.add_argument("--drain-seconds", type=float, default=600.0)
    parser.add_argument("--label", default=os.environ.get("PROBE_LABEL", "probe"))
    parser.add_argument("--long-tasks", type=int, default=0, help="long tasks started first")
    parser.add_argument("--rounds", type=int, default=10, help="rounds per long task")
    parser.add_argument("--lead-seconds", type=float, default=5.0)
    arguments = parser.parse_args()
    result = probe(
        arguments.rate,
        arguments.seconds,
        arguments.drain_seconds,
        arguments.label,
        long_tasks=arguments.long_tasks,
        rounds=arguments.rounds,
        lead_seconds=arguments.lead_seconds,
    )
    _report(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
