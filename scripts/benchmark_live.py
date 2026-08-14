"""Live drivers for the M1 §24.1 gates. Thresholds live in benchmark_m1.py.

Each driver measures what the product table named. A missing sandbox image, a
short ``--seconds`` sample, a sequence gap, or a changed container id is a
measured failure. Nothing here lowers a cell or offers a skip.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 - the drivers observe Docker and Compose
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote
from uuid import UUID, uuid4

import httpx

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from restart_drill import (  # noqa: E402
    API,
    CSRF_COOKIE,
    await_healthy,
    compose,
    containers_for_drill,
)
from workspace_drill import WorkspaceConsole  # noqa: E402

PRELOAD_RUN_EVENTS = 100_000
COLD_SAMPLES = 12
WARM_SAMPLES = 5
SMALL_SAMPLES = 12
NEXT_SAMPLES = 5
SSE_CADENCE_S = 5.0
SSE_CADENCE_SLACK_S = 2.0
WARM_COMMAND = "sleep 35"
RECOVERY_COMMAND = "sleep 90"
COLD_COMMAND = "true"
SMALL_COMMAND = "dd if=/dev/zero of=data.bin bs=1M count=1 2>/dev/null && sync"
LARGE_COMMAND = (
    "mkdir -p tree && cd tree && "
    "for i in $(seq 1 1000); do head -c 104858 /dev/zero > f$i.bin; done"
)
LARGE_PROBE = "test -e tree/f1000.bin"
NEXT_PROBE = "test -e data.bin"

TOOL_GATES = frozenset(
    {
        "sandbox_cold",
        "sandbox_warm",
        "workspace_small",
        "workspace_large",
        "next_run",
        "worker_recovery",
    }
)

RESERVE_SEQUENCES = """
UPDATE runs SET next_event_sequence = next_event_sequence + $1
WHERE id = $2 AND workspace_id = $3
RETURNING next_event_sequence - $1 AS first_sequence
"""

INSERT_EVENT = """
INSERT INTO run_events (id, run_id, workspace_id, sequence, event_type, payload, occurred_at)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
"""


class EventStore(Protocol):
    def write_one(self, run_id: str, workspace_id: str) -> float: ...
    def write_many(self, run_id: str, workspace_id: str, count: int) -> None: ...
    def sequences(self, run_id: str) -> list[int]: ...
    def event_types_after(self, run_id: str, after: int) -> list[str]: ...
    def event_count(self) -> int: ...
    def sandbox(self, run_id: str) -> tuple[str, str, str, str] | None: ...
    def run_status(self, run_id: str) -> str | None: ...


class BenchmarkConsole(WorkspaceConsole):
    """The workspace drill's console, plus a create that reports its own latency."""

    def create_run(
        self, workspace: str, session: str, text: str, key: str
    ) -> tuple[str | None, int, float]:
        return post_run(
            str(self._client.base_url),
            {name: value for name, value in self._client.cookies.items()},
            workspace,
            session,
            text,
            key,
        )

    def cookies(self) -> dict[str, str]:
        return {name: value for name, value in self._client.cookies.items()}

    def csrf(self) -> str:
        return unquote(self._client.cookies.get(CSRF_COOKIE) or "")

    def cancel(self, workspace: str, run: str) -> None:
        version = int(self.read(workspace, run)["state_version"])
        answer = self._client.post(
            f"/api/v1/runs/{run}/cancel",
            headers=self._headers(workspace),
            json={"expected_state_version": version},
        )
        answer.raise_for_status()


@dataclass
class Harness:
    open_console: Callable[[], Any]
    open_store: Callable[[], Any]
    compose: Callable[..., None]
    await_healthy: Callable[..., None]
    digest: Callable[[], str]
    containers: Callable[[], set[str]]
    inspect: Callable[[str], dict[str, str]]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    api: str
    subscribe: Callable[..., None] | None = None


def planned_seconds(gate: Any, seconds: int | None) -> float | None:
    if gate.duration_s is None:
        return None
    if seconds is None:
        return float(gate.duration_s)
    return float(min(gate.duration_s, max(seconds, 0)))


def sequence_loss(sequences: list[int]) -> int:
    if not sequences:
        return 0
    unique = sorted(set(sequences))
    return unique[-1] - unique[0] + 1 - len(unique)


def reconnect_gap(last_seen: int, first_replayed: int) -> int:
    return max(0, first_replayed - last_seen - 1)


def missed_cadence(
    stamps: list[float],
    every: float,
    slack: float,
    *,
    skip_first: int = 1,
) -> bool:
    """True when a live gap exceeds 5s + slack.

    The first interval is setup: history replay and the subscriber joining.
    The product cell is the hold after that, not the connect storm.
    """
    live = stamps[skip_first:]
    if len(live) < 2:
        return True
    return any(
        later - earlier > every + slack
        for earlier, later in zip(live, live[1:], strict=False)
    )


def same_container(left: str, right: str) -> bool:
    """Docker ps may print a 12-char prefix of the inspect id."""
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def warm_reasons(before_id: str, after_id: str, reset_after_first: bool) -> list[str]:
    reasons: list[str] = []
    if not same_container(before_id, after_id):
        reasons.append(f"container id changed {before_id} -> {after_id}")
    if reset_after_first:
        reasons.append("second acquire wrote sandbox_cache_reset (not a warm reuse)")
    return reasons


def parse_sse_id(line: str) -> int | None:
    if not line.startswith("id:"):
        return None
    try:
        return int(line.split(":", 1)[1].strip())
    except ValueError:
        return None


def post_run(
    base_url: str,
    cookies: dict[str, str],
    workspace: str,
    session: str,
    text: str,
    key: str,
) -> tuple[str | None, int, float]:
    started = time.perf_counter()
    with httpx.Client(
        base_url=base_url, cookies=cookies, timeout=30.0, trust_env=False
    ) as client:
        token = unquote(client.cookies.get(CSRF_COOKIE) or "")
        answer = client.post(
            "/api/v1/runs",
            headers={
                "X-CSRF-Token": token,
                "X-Workspace-Id": workspace,
                "Idempotency-Key": key,
            },
            json={"session_id": session, "input": text},
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if answer.status_code >= 400:
        return None, answer.status_code, elapsed_ms
    return str(answer.json()["id"]), answer.status_code, elapsed_ms


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _fail(
    gate: Any,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    why: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return evaluate(
        gate,
        sample_type(latencies_ms=(), errors=1, total=1),
        extra_reasons=[why],
        **kwargs,
    )


def _pace(harness: Harness, rps: int, duration: float) -> Iterator[int]:
    interval = 1.0 / rps
    started = harness.monotonic()
    next_at = started
    deadline = started + duration
    index = 0
    while next_at < deadline:
        now = harness.monotonic()
        if now < next_at:
            harness.sleep(next_at - now)
        yield index
        index += 1
        next_at += interval


def drive_create_run(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    duration = planned_seconds(gate, seconds) or 0.0
    sessions_n = gate.sessions or 1
    rps = gate.rps or 1
    with harness.open_console() as console:
        console.sign_in()
        workspace = console.create_workspace(f"bench-create-{time.time_ns()}")
        agent = console.publish_agent(
            workspace, f"bench-create-{time.time_ns()}", "complete"
        )
        sessions = [console.open_session(workspace, agent) for _ in range(sessions_n)]
        latencies: list[float] = []
        errors = 0
        lock = threading.Lock()

        def submit(index: int) -> None:
            nonlocal errors
            session = sessions[index % len(sessions)]
            run_id, status, latency = console.create_run(
                workspace,
                session,
                "benchmark",
                f"bench-create-{time.time_ns()}-{index}",
            )
            with lock:
                if run_id is None or status >= 400:
                    errors += 1
                else:
                    latencies.append(latency)

        started = harness.monotonic()
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(submit, index) for index in _pace(harness, rps, duration)]
            for future in as_completed(futures):
                future.result()
        elapsed = harness.monotonic() - started
        total = len(futures)
        achieved = (total / elapsed) if elapsed else 0.0
        _log(f"create_run n={total} sessions={len(sessions)} {elapsed:.1f}s")
        return evaluate(
            gate,
            sample_type(latencies_ms=tuple(latencies), errors=errors, total=total),
            achieved_rps=achieved,
            sampled_s=elapsed,
        )


def drive_run_event(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    duration = planned_seconds(gate, seconds) or 0.0
    rps = gate.rps or 1
    extra: list[str] = []
    with harness.open_console() as console, harness.open_store() as store:
        console.sign_in()
        workspace = console.create_workspace(f"bench-events-{time.time_ns()}")
        agent = console.publish_agent(
            workspace, f"bench-events-{time.time_ns()}", "complete"
        )
        session = console.open_session(workspace, agent)
        run_id, status, _ = console.create_run(
            workspace, session, "benchmark", f"bench-events-{time.time_ns()}"
        )
        if run_id is None or status >= 400:
            return _fail(gate, evaluate, sample_type, f"could not create a run ({status})")
        missing = PRELOAD_RUN_EVENTS - store.event_count()
        if missing > 0:
            _log(f"run_event preloading {missing} events")
            store.write_many(run_id, workspace, missing)
        latencies: list[float] = []
        started = harness.monotonic()
        for _ in _pace(harness, rps, duration):
            latencies.append(store.write_one(run_id, workspace))
        elapsed = harness.monotonic() - started
        lost = sequence_loss(store.sequences(run_id))
        if lost:
            extra.append(f"gap in committed sequences: lost {lost}")
        achieved = (len(latencies) / elapsed) if elapsed else 0.0
        _log(f"run_event n={len(latencies)} lost={lost} {elapsed:.1f}s")
        return evaluate(
            gate,
            sample_type(
                latencies_ms=tuple(latencies), errors=lost, total=len(latencies)
            ),
            achieved_rps=achieved,
            sampled_s=elapsed,
            extra_reasons=extra or None,
        )


def drive_sse(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    duration = planned_seconds(gate, seconds) or 0.0
    connections = gate.connections or 1
    extra: list[str] = []
    harness.compose("stop", "worker")
    try:
        return _drive_sse_held(
            gate,
            duration,
            connections,
            extra,
            evaluate=evaluate,
            sample_type=sample_type,
            harness=harness,
        )
    finally:
        harness.compose("start", "worker")
        harness.await_healthy("worker")


def _drive_sse_held(
    gate: Any,
    duration: float,
    connections: int,
    extra: list[str],
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    with harness.open_console() as console, harness.open_store() as store:
        console.sign_in()
        workspace = console.create_workspace(f"bench-sse-{time.time_ns()}")
        agent = console.publish_agent(workspace, f"bench-sse-{time.time_ns()}", "complete")
        session = console.open_session(workspace, agent)
        run_id, status, _ = console.create_run(
            workspace, session, "sse-hold", f"bench-sse-{time.time_ns()}"
        )
        if run_id is None or status >= 400:
            return _fail(gate, evaluate, sample_type, f"could not create a run ({status})")
        store.write_one(run_id, workspace)
        stop = threading.Event()
        buckets: list[list[tuple[float, int]]] = [[] for _ in range(connections)]
        url = f"{harness.api}/api/v1/runs/{run_id}/events?workspace_id={workspace}"
        headers = {"Accept": "text/event-stream"}
        cookies = console.cookies()

        def worker(index: int) -> None:
            _subscribe(
                harness, url, headers, cookies, None, stop, buckets[index]
            )

        with ThreadPoolExecutor(max_workers=connections) as pool:
            futures = [pool.submit(worker, index) for index in range(connections)]
            attach_deadline = harness.monotonic() + 30.0
            while harness.monotonic() < attach_deadline:
                attached = sum(1 for bucket in buckets if bucket)
                if attached >= connections:
                    break
                harness.sleep(0.05)
            started = harness.monotonic()
            deadline = started + duration
            tick = 0
            while harness.monotonic() < deadline:
                store.write_one(run_id, workspace)
                tick += 1
                target = started + tick * SSE_CADENCE_S
                now = harness.monotonic()
                if now < target and now < deadline:
                    harness.sleep(min(target - now, deadline - now))
            expected = max(1, int(duration / SSE_CADENCE_S))
            for index, bucket in enumerate(buckets):
                stamps = [stamp for stamp, _ in bucket]
                seqs = [sequence for _, sequence in bucket]
                if len(bucket) < expected:
                    extra.append(
                        f"connection {index} received {len(bucket)} events, "
                        f"expected >= {expected}"
                    )
                if missed_cadence(stamps, SSE_CADENCE_S, SSE_CADENCE_SLACK_S):
                    extra.append(f"connection {index} missed the 5s cadence")
                lost = sequence_loss(seqs)
                if lost:
                    extra.append(f"connection {index} had a committed gap of {lost}")
            last_seen = buckets[0][-1][1] if buckets[0] else 0
            store.write_one(run_id, workspace)
            replayed: list[tuple[float, int]] = []
            replay_stop = threading.Event()
            _subscribe(
                harness, url, headers, cookies, str(last_seen), replay_stop, replayed
            )
            replay_stop.set()
            if not replayed:
                extra.append("reconnect delivered no event")
            else:
                gap = reconnect_gap(last_seen, replayed[0][1])
                if gap:
                    extra.append(f"reconnect skipped {gap} committed event(s)")
            stop.set()
            store.write_one(run_id, workspace)
            for future in futures:
                future.result(timeout=20)
        elapsed = harness.monotonic() - started
        _log(f"sse connections={connections} {elapsed:.1f}s reasons={len(extra)}")
        return evaluate(
            gate,
            sample_type(
                latencies_ms=tuple(
                    float(len(bucket)) for bucket in buckets if bucket
                ),
                errors=len(extra),
                total=connections,
            ),
            sampled_s=elapsed,
            extra_reasons=extra or None,
        )


def drive_sandbox_cold(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    latencies: list[float] = []
    errors = 0
    with harness.open_console() as console, harness.open_store() as store:
        workspace, agent = _tool_agent(console, "cold")
        for index in range(COLD_SAMPLES):
            session = console.open_session(workspace, agent)
            before = harness.containers()
            started = time.perf_counter()
            run_id, status, _ = console.create_run(
                workspace, session, COLD_COMMAND, f"bench-cold-{time.time_ns()}-{index}"
            )
            if run_id is None or status >= 400:
                errors += 1
                extra.append(f"cold start {index} was not accepted")
                continue
            appeared = _wait_container(harness, store, run_id, before, 60.0)
            latency = (time.perf_counter() - started) * 1000.0
            if appeared is None:
                errors += 1
                extra.append(f"cold start {index} never produced a container")
                continue
            latencies.append(latency)
    _log(f"sandbox_cold n={len(latencies)} errors={errors}")
    return evaluate(
        gate,
        sample_type(latencies_ms=tuple(latencies), errors=errors, total=COLD_SAMPLES),
        extra_reasons=extra or None,
    )


def drive_sandbox_warm(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    latencies: list[float] = []
    with harness.open_console() as console, harness.open_store() as store:
        workspace, agent = _tool_agent(console, "warm")
        for index in range(WARM_SAMPLES):
            session = console.open_session(workspace, agent)
            before = harness.containers()
            run_id, status, _ = console.create_run(
                workspace, session, WARM_COMMAND, f"bench-warm-{time.time_ns()}-{index}"
            )
            if run_id is None or status >= 400:
                extra.append(f"warm sample {index} was not accepted")
                continue
            original = _wait_container(harness, store, run_id, before, 60.0)
            if original is None:
                extra.append(f"warm sample {index} never started a container")
                continue
            latency, reasons = _measure_thaw(harness, store, run_id, original)
            extra.extend(reasons)
            if latency is not None:
                latencies.append(latency)
    _log(f"sandbox_warm n={len(latencies)} reasons={len(extra)}")
    return evaluate(
        gate,
        sample_type(
            latencies_ms=tuple(latencies),
            errors=0 if latencies and not extra else 1,
            total=max(len(latencies), 1),
        ),
        extra_reasons=extra or None,
    )


def drive_workspace_small(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    latencies: list[float] = []
    errors = 0
    with harness.open_console() as console:
        workspace, agent = _tool_agent(console, "ws-small")
        for index in range(SMALL_SAMPLES):
            session = console.open_session(workspace, agent)
            started = time.perf_counter()
            run_id, status, _ = console.create_run(
                workspace, session, SMALL_COMMAND, f"bench-small-{time.time_ns()}-{index}"
            )
            if run_id is None or status >= 400:
                errors += 1
                extra.append(f"small commit {index} was not accepted")
                continue
            try:
                snapshot, _ = console.await_status(
                    workspace, run_id, ("completed", "failed"), 180.0
                )
            except SystemExit as error:
                errors += 1
                extra.append(str(error))
                continue
            latency = (time.perf_counter() - started) * 1000.0
            if snapshot["status"] != "completed":
                errors += 1
                extra.append(f"small commit {index} ended {snapshot['status']}")
                continue
            latencies.append(latency)
    return evaluate(
        gate,
        sample_type(latencies_ms=tuple(latencies), errors=errors, total=SMALL_SAMPLES),
        extra_reasons=extra or None,
    )


def drive_workspace_large(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    with harness.open_console() as console:
        workspace, agent = _tool_agent(console, "ws-large")
        session = console.open_session(workspace, agent)
        started = time.perf_counter()
        run_id, status, _ = console.create_run(
            workspace, session, LARGE_COMMAND, f"bench-large-{time.time_ns()}"
        )
        if run_id is None or status >= 400:
            return _fail(gate, evaluate, sample_type, "large commit was not accepted")
        try:
            snapshot, _ = console.await_status(
                workspace, run_id, ("completed", "failed"), 300.0
            )
        except SystemExit as error:
            return _fail(gate, evaluate, sample_type, str(error))
        latency = (time.perf_counter() - started) * 1000.0
        if snapshot["status"] != "completed":
            extra.append(f"large commit ended {snapshot['status']}")
        probe, probe_status, _ = console.create_run(
            workspace, session, LARGE_PROBE, f"bench-large-probe-{time.time_ns()}"
        )
        if probe is None or probe_status >= 400:
            extra.append("checkpoint probe was not accepted")
        else:
            try:
                proved, _ = console.await_status(
                    workspace, probe, ("completed", "failed"), 180.0
                )
            except SystemExit as error:
                extra.append(str(error))
            else:
                if proved["status"] != "completed":
                    extra.append("large commit was not visible to the next Run")
        return evaluate(
            gate,
            sample_type(
                latencies_ms=(latency,),
                errors=len(extra),
                total=1,
            ),
            extra_reasons=extra or None,
        )


def drive_next_run(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    latencies: list[float] = []
    errors = 0
    with harness.open_console() as console, harness.open_store() as store:
        workspace, agent = _tool_agent(console, "next")
        for index in range(NEXT_SAMPLES):
            session = console.open_session(workspace, agent)
            first, status, _ = console.create_run(
                workspace, session, SMALL_COMMAND, f"bench-next-a-{time.time_ns()}-{index}"
            )
            if first is None or status >= 400:
                errors += 1
                extra.append(f"next-run setup {index} was not accepted")
                continue
            before = harness.containers()
            first_box = _wait_container(harness, store, first, before, 60.0)
            try:
                snapshot, _ = console.await_status(
                    workspace, first, ("completed", "failed"), 180.0
                )
            except SystemExit as error:
                errors += 1
                extra.append(str(error))
                continue
            if snapshot["status"] != "completed":
                errors += 1
                extra.append(f"next-run setup {index} ended {snapshot['status']}")
                continue
            image = ""
            if first_box and first_box not in {"event"}:
                image = harness.inspect(first_box).get("image", "")
            started = time.perf_counter()
            second, second_status, _ = console.create_run(
                workspace, session, NEXT_PROBE, f"bench-next-b-{time.time_ns()}-{index}"
            )
            if second is None or second_status >= 400:
                errors += 1
                extra.append(f"next run {index} was not accepted")
                continue
            after = harness.containers()
            second_box = _wait_container(harness, store, second, after, 60.0)
            latency = (time.perf_counter() - started) * 1000.0
            if second_box is None:
                errors += 1
                extra.append(f"next run {index} never reached a first tool")
                continue
            latencies.append(latency)
            if (
                first_box
                and second_box
                and first_box != "event"
                and second_box != "event"
            ):
                if first_box == second_box:
                    extra.append("next run reused the previous writable layer")
                if image:
                    later = harness.inspect(second_box).get("image", "")
                    if later and later != image:
                        extra.append("next run did not reuse the cached readonly layer")
            try:
                proved, _ = console.await_status(
                    workspace, second, ("completed", "failed"), 180.0
                )
            except SystemExit as error:
                extra.append(str(error))
            else:
                if proved["status"] != "completed":
                    extra.append("next run did not see the previous 1 MiB commit")
    return evaluate(
        gate,
        sample_type(latencies_ms=tuple(latencies), errors=errors, total=NEXT_SAMPLES),
        extra_reasons=extra or None,
    )


def drive_worker_recovery(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    with harness.open_console() as console:
        workspace, agent = _tool_agent(console, "recovery")
        session = console.open_session(workspace, agent)
        run_id, status, _ = console.create_run(
            workspace, session, RECOVERY_COMMAND, f"bench-wrec-{time.time_ns()}"
        )
        if run_id is None or status >= 400:
            return _fail(gate, evaluate, sample_type, "recovery run was not accepted")
        try:
            console.await_status(workspace, run_id, ("running",), 120.0)
        except SystemExit as error:
            return _fail(gate, evaluate, sample_type, str(error))
        started = harness.monotonic()
        # Do not restart the Scheduler: compose recreates it through migrate
        # and leaves nobody scanning while the lease is expiring. The cell is
        # "kill the Worker → queued", with the already-healthy Scheduler.
        harness.compose("kill", "worker")
        try:
            snapshot, waited = console.await_status(
                workspace, run_id, ("queued",), 120.0
            )
        except SystemExit as error:
            extra.append(str(error))
            waited = 120.0
            snapshot = {"status": "unknown"}
        elapsed = max(harness.monotonic() - started, waited)
        if snapshot.get("status") != "queued":
            extra.append(f"run re-entered {snapshot.get('status')!r}, not queued")
        try:
            console.cancel(workspace, run_id)
        except Exception as error:  # noqa: BLE001 - leftover work must not hide the gate
            _log(f"worker_recovery cancel failed: {error}")
        try:
            harness.compose("start", "worker")
            harness.await_healthy("worker")
        except SystemExit as error:
            extra.append(str(error))
        _log(f"worker_recovery queued_in={elapsed:.2f}s")
        return evaluate(
            gate,
            sample_type(latencies_ms=(elapsed * 1000.0,), errors=len(extra), total=1),
            elapsed_s=elapsed,
            extra_reasons=extra or None,
        )


def drive_service_recovery(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness,
) -> dict[str, Any]:
    extra: list[str] = []
    cap = gate.max_s or 60.0
    started = harness.monotonic()
    harness.compose("restart", "api")
    harness.await_healthy("api")
    with harness.open_console() as console:
        console.sign_in()
        workspace = console.create_workspace(f"bench-srec-{time.time_ns()}")
        agent = console.publish_agent(
            workspace, f"bench-srec-{time.time_ns()}", "complete"
        )
        session = console.open_session(workspace, agent)
        run_id, status, _ = console.create_run(
            workspace, session, "benchmark", f"bench-srec-{time.time_ns()}"
        )
        if run_id is None or status >= 400:
            extra.append("new run was not accepted after the API restart")
        else:
            remaining = cap - (harness.monotonic() - started)
            if remaining <= 0:
                extra.append("health check consumed the recovery window")
            else:
                try:
                    console.await_status(
                        workspace, run_id, ("running", "completed"), remaining
                    )
                except SystemExit as error:
                    extra.append(str(error))
    elapsed = harness.monotonic() - started
    _log(f"service_recovery elapsed={elapsed:.2f}s")
    return evaluate(
        gate,
        sample_type(latencies_ms=(elapsed * 1000.0,), errors=len(extra), total=1),
        elapsed_s=elapsed,
        extra_reasons=extra or None,
    )


DRIVERS: dict[str, Callable[..., dict[str, Any]]] = {
    "create_run": drive_create_run,
    "run_event": drive_run_event,
    "sse": drive_sse,
    "sandbox_cold": drive_sandbox_cold,
    "sandbox_warm": drive_sandbox_warm,
    "workspace_small": drive_workspace_small,
    "workspace_large": drive_workspace_large,
    "next_run": drive_next_run,
    "worker_recovery": drive_worker_recovery,
    "service_recovery": drive_service_recovery,
}


def run_driver(
    gate: Any,
    seconds: int | None,
    *,
    evaluate: Callable[..., dict[str, Any]],
    sample_type: Callable[..., Any],
    harness: Harness | None = None,
) -> dict[str, Any]:
    used = harness or default_harness()
    driver = DRIVERS.get(gate.name)
    if driver is None:
        return _fail(gate, evaluate, sample_type, f"no live driver for {gate.name}")
    if gate.name in TOOL_GATES and not used.digest():
        return _fail(
            gate,
            evaluate,
            sample_type,
            "SANDBOX_IMAGE_DIGEST is not set; tool gates cannot run",
        )
    return driver(gate, seconds, evaluate=evaluate, sample_type=sample_type, harness=used)


def default_harness() -> Harness:
    return Harness(
        open_console=_open_console,
        open_store=_open_store,
        compose=_compose,
        await_healthy=await_healthy,
        digest=lambda: os.environ.get("SANDBOX_IMAGE_DIGEST", ""),
        containers=containers_for_drill,
        inspect=_inspect_container,
        monotonic=time.monotonic,
        sleep=time.sleep,
        api=API,
    )


def _compose(*arguments: str) -> None:
    """Compose chatter goes to stderr so the official JSON stays parseable."""
    with redirect_stdout(sys.stderr):
        compose(*arguments)


@contextmanager
def _open_console() -> Generator[BenchmarkConsole]:
    with httpx.Client(base_url=API, timeout=30.0, trust_env=False) as client:
        yield BenchmarkConsole(client)


@contextmanager
def _open_store() -> Generator[PostgresEventStore]:
    store = PostgresEventStore(_database_url())
    store.open()
    try:
        yield store
    finally:
        store.close()


def _database_url() -> str:
    raw = os.environ.get(
        "TINY_HERMES_BENCHMARK_DATABASE",
        "postgresql://tiny_hermes:local-only@127.0.0.1:5432/tiny_hermes",
    )
    return raw.replace("postgresql+asyncpg://", "postgresql://")


def _tool_agent(console: Any, label: str) -> tuple[str, str]:
    console.sign_in()
    workspace = console.create_workspace(f"bench-{label}-{time.time_ns()}")
    agent = console.publish_agent(
        workspace,
        f"bench-{label}-{time.time_ns()}",
        "shell_from_input",
        tools=["shell.exec"],
    )
    return workspace, agent


def _wait_container(
    harness: Harness,
    store: EventStore,
    run_id: str,
    before: set[str],
    timeout: float,
) -> str | None:
    deadline = harness.monotonic() + timeout
    while harness.monotonic() < deadline:
        appeared = harness.containers() - before
        if appeared:
            return next(iter(appeared))
        row = store.sandbox(run_id)
        if row is not None:
            return row[0]
        if "sandbox_cache_reset" in store.event_types_after(run_id, 0):
            return "event"
        harness.sleep(0.05)
    return None


def _measure_thaw(
    harness: Harness,
    store: EventStore,
    run_id: str,
    original_id: str,
) -> tuple[float | None, list[str]]:
    """Time keep → thaw after the slice ends, not the first freeze of the Run.

    A cold acquire may freeze for workspace restore. Starting the clock there
    measures the rest of `sleep 35`, not the warm reacquire.
    """
    deadline = harness.monotonic() + 90.0
    while harness.monotonic() < deadline:
        if "run_slice_ended" in store.event_types_after(run_id, 0):
            break
        harness.sleep(0.05)
    else:
        return None, ["slice never ended; warm reacquire was not exercised"]
    # The first slice is still `running` until freeze+keep commit. Starting
    # the clock on that row measures leftover command time, not the thaw.
    while harness.monotonic() < deadline:
        parked = store.sandbox(run_id)
        if parked is not None and (
            parked[1] == "frozen" or parked[3] == "kept"
        ):
            break
        harness.sleep(0.01)
    else:
        return None, ["slice ended but the instance was never kept warm"]
    started = time.perf_counter()
    while harness.monotonic() < deadline:
        row = store.sandbox(run_id)
        if row is not None:
            container_id, instance_status, _digest, _reservation = row
            if instance_status == "running":
                latency = (time.perf_counter() - started) * 1000.0
                resets = store.event_types_after(run_id, 0).count("sandbox_cache_reset")
                return latency, warm_reasons(original_id, container_id, resets > 1)
        harness.sleep(0.01)
    return None, ["warm reacquire never observed freeze then thaw"]


def _subscribe(
    harness: Harness,
    url: str,
    headers: dict[str, str],
    cookies: dict[str, str],
    last_event_id: str | None,
    stop: threading.Event,
    into: list[tuple[float, int]],
) -> None:
    if harness.subscribe is not None:
        harness.subscribe(url, headers, cookies, last_event_id, stop, into)
        return
    request_headers = dict(headers)
    if last_event_id:
        request_headers["Last-Event-ID"] = last_event_id
    with httpx.Client(timeout=900.0, trust_env=False, cookies=cookies) as client:
        with client.stream("GET", url, headers=request_headers) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if stop.is_set():
                    return
                parsed = parse_sse_id(line)
                if parsed is not None:
                    into.append((time.monotonic(), parsed))
                if last_event_id is not None and parsed is not None:
                    return


def _inspect_container(container_id: str) -> dict[str, str]:
    found = subprocess.run(  # noqa: S603 - container id came from docker
        [  # noqa: S607
            "docker",
            "inspect",
            "--format",
            "{{.Id}} {{.State.Paused}} {{.Image}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode != 0:
        return {"id": container_id, "paused": "unknown", "image": ""}
    parts = found.stdout.split()
    image = parts[2] if len(parts) > 2 else ""
    paused = parts[1] if len(parts) > 1 else ""
    return {"id": parts[0] if parts else container_id, "paused": paused, "image": image}


async def _asyncpg_connect(url: str) -> Any:
    import asyncpg  # type: ignore[import-untyped]

    return await asyncpg.connect(url)  # type: ignore[no-any-return]


class PostgresEventStore:
    """The same allocator SqlRunStore.append_events uses, over a live connection."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn: Any = None

    def open(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._conn = self._loop.run_until_complete(_asyncpg_connect(self._url))

    def close(self) -> None:
        if self._loop is None:
            return
        if self._conn is not None:
            self._loop.run_until_complete(self._conn.close())
        self._loop.close()
        self._loop = None
        self._conn = None

    def _go(self, coro: Any) -> Any:
        if self._loop is None:
            raise RuntimeError("event store is closed")
        return self._loop.run_until_complete(coro)

    def write_one(self, run_id: str, workspace_id: str) -> float:
        async def _inner() -> float:
            started = time.perf_counter()
            await self._write(run_id, workspace_id, 1)
            return (time.perf_counter() - started) * 1000.0

        return float(self._go(_inner()))

    def write_many(self, run_id: str, workspace_id: str, count: int) -> None:
        async def _inner() -> None:
            remaining = count
            while remaining:
                chunk = min(remaining, 2_000)
                await self._write(run_id, workspace_id, chunk)
                remaining -= chunk

        self._go(_inner())

    def sequences(self, run_id: str) -> list[int]:
        async def _inner() -> list[int]:
            rows = await self._conn.fetch(
                "SELECT sequence FROM run_events WHERE run_id = $1 ORDER BY sequence",
                UUID(run_id),
            )
            return [int(row["sequence"]) for row in rows]

        return list(self._go(_inner()))

    def event_types_after(self, run_id: str, after: int) -> list[str]:
        async def _inner() -> list[str]:
            rows = await self._conn.fetch(
                "SELECT event_type FROM run_events "
                "WHERE run_id = $1 AND sequence > $2 ORDER BY sequence",
                UUID(run_id),
                after,
            )
            return [str(row["event_type"]) for row in rows]

        return list(self._go(_inner()))

    def event_count(self) -> int:
        async def _inner() -> int:
            value = await self._conn.fetchval("SELECT count(*) FROM run_events")
            return int(value or 0)

        return int(self._go(_inner()))

    def sandbox(self, run_id: str) -> tuple[str, str, str, str] | None:
        async def _inner() -> tuple[str, str, str, str] | None:
            row = await self._conn.fetchrow(
                "SELECT i.container_id, i.status, i.image_digest, r.status AS reservation "
                "FROM sandbox_reservations r "
                "JOIN sandbox_instances i ON i.id = r.sandbox_instance_id "
                "WHERE r.run_id = $1 ORDER BY r.created_at DESC LIMIT 1",
                UUID(run_id),
            )
            if row is None:
                return None
            return (
                str(row["container_id"]),
                str(row["status"]),
                str(row["image_digest"]),
                str(row["reservation"]),
            )

        return self._go(_inner())

    def run_status(self, run_id: str) -> str | None:
        async def _inner() -> str | None:
            value = await self._conn.fetchval(
                "SELECT status FROM runs WHERE id = $1", UUID(run_id)
            )
            return None if value is None else str(value)

        return self._go(_inner())

    async def _write(self, run_id: str, workspace_id: str, count: int) -> int:
        run = UUID(run_id)
        workspace = UUID(workspace_id)
        async with self._conn.transaction():
            first = await self._conn.fetchval(RESERVE_SEQUENCES, count, run, workspace)
            if first is None:
                raise RuntimeError("run not found while reserving event sequences")
            occurred = datetime.now(UTC)
            rows = [
                (uuid4(), run, workspace, int(first) + offset, "run_created", "{}", occurred)
                for offset in range(count)
            ]
            await self._conn.executemany(INSERT_EVENT, rows)
        return int(first)
