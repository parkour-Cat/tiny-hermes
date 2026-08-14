"""M1 §24.1 benchmark. Failures are not fixed by editing the thresholds.

The product table is the gate. This file copies those cells as frozen
constants. A Cloud Agent with 4 vCPU, or any host whose API is down, must
exit nonzero and must not print a pass.

Usage::

    uv run --no-sync python scripts/benchmark_m1.py --shape-only
    uv run --no-sync python scripts/benchmark_m1.py
    DETERMINISTIC_MODEL_DELAY_MS=50 uv run --no-sync python scripts/benchmark_m1.py \\
        --gate create_run
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

API = os.environ.get("TINY_HERMES_API", "http://127.0.0.1:8000")

REFERENCE_VCPU = 8
REFERENCE_RAM_GIB = 16
DETERMINISTIC_DELAY_MS = 50
PRELOAD_RUN_EVENTS = 100_000


@dataclass(frozen=True)
class Gate:
    name: str
    p95_ms: float | None = None
    error_rate: float | None = None
    max_loss: int | None = None
    max_s: float | None = None
    rps: int | None = None
    duration_s: int | None = None
    sessions: int | None = None
    connections: int | None = None


CREATE_RUN = Gate(
    name="create_run",
    sessions=200,
    rps=20,
    duration_s=300,
    p95_ms=300.0,
    error_rate=0.001,
)
RUN_EVENT = Gate(name="run_event", rps=500, duration_s=300, p95_ms=100.0, max_loss=0)
SSE = Gate(name="sse", connections=500, duration_s=600)
SANDBOX_COLD = Gate(name="sandbox_cold", p95_ms=3000.0)
SANDBOX_WARM = Gate(name="sandbox_warm", p95_ms=300.0)
WORKSPACE_SMALL = Gate(name="workspace_small", p95_ms=1000.0)
WORKSPACE_LARGE = Gate(name="workspace_large", p95_ms=15000.0)
NEXT_RUN = Gate(name="next_run", p95_ms=3000.0)
WORKER_RECOVERY = Gate(name="worker_recovery", max_s=30.0)
SERVICE_RECOVERY = Gate(name="service_recovery", max_s=60.0)

GATES: tuple[Gate, ...] = (
    CREATE_RUN,
    RUN_EVENT,
    SSE,
    SANDBOX_COLD,
    SANDBOX_WARM,
    WORKSPACE_SMALL,
    WORKSPACE_LARGE,
    NEXT_RUN,
    WORKER_RECOVERY,
    SERVICE_RECOVERY,
)


@dataclass(frozen=True)
class Shape:
    os: str
    vcpu: int
    ram_gib: float


@dataclass(frozen=True)
class Sample:
    latencies_ms: tuple[float, ...]
    errors: int
    total: int


@dataclass(frozen=True)
class Usage:
    cpu_percent: float
    rss_mib: float


def percentile(values: tuple[float, ...] | list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def matches_reference(shape: Shape) -> bool:
    # MemTotal is a few hundred MiB short of the marketed size. Compare the
    # rounded GiB so a 16 GB reference host is not rejected for kernel pages.
    ram = int(shape.ram_gib + 0.5)
    return shape.os == "linux" and shape.vcpu >= REFERENCE_VCPU and ram >= REFERENCE_RAM_GIB


def host_shape() -> Shape:
    ram_gib = 0.0
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kilobytes = float(line.split()[1])
                ram_gib = kilobytes / 1024.0 / 1024.0
                break
    return Shape(
        os=platform.system().lower(),
        vcpu=os.cpu_count() or 0,
        ram_gib=ram_gib,
    )


def git_sha() -> str:
    head = Path(".git/HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = Path(".git") / head.removeprefix("ref: ")
        return ref.read_text(encoding="utf-8").strip()
    return head


def process_usage() -> Usage:
    rss_mib = 0.0
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss_mib = float(line.split()[1]) / 1024.0
                break
    return Usage(cpu_percent=0.0, rss_mib=rss_mib)


def api_ready() -> bool:
    try:
        import httpx
    except ImportError:
        return False
    try:
        answer = httpx.get(f"{API}/health/ready", timeout=2.0)
    except httpx.HTTPError:
        return False
    return answer.status_code == 200


def evaluate(
    gate: Gate,
    sample: Sample,
    *,
    elapsed_s: float | None = None,
    achieved_rps: float | None = None,
    sampled_s: float | None = None,
    extra_reasons: list[str] | None = None,
) -> dict[str, Any]:
    p50 = percentile(sample.latencies_ms, 50.0)
    p95 = percentile(sample.latencies_ms, 95.0)
    p99 = percentile(sample.latencies_ms, 99.0)
    rate = (sample.errors / sample.total) if sample.total else 1.0
    reasons: list[str] = []
    if gate.p95_ms is not None and p95 > gate.p95_ms:
        reasons.append(f"p95 {p95:.1f}ms exceeds {gate.p95_ms}ms")
    if gate.error_rate is not None and rate > gate.error_rate:
        reasons.append(f"error rate {rate:.4f} exceeds {gate.error_rate}")
    if gate.max_loss is not None and sample.errors > gate.max_loss:
        reasons.append(f"lost {sample.errors} events; max_loss is {gate.max_loss}")
    if gate.max_s is not None and elapsed_s is not None and elapsed_s > gate.max_s:
        reasons.append(f"elapsed {elapsed_s:.2f}s exceeds {gate.max_s}s")
    if gate.rps is not None and achieved_rps is not None and achieved_rps < gate.rps:
        reasons.append(f"achieved {achieved_rps:.1f}/s below {gate.rps}/s")
    if (
        gate.duration_s is not None
        and sampled_s is not None
        and sampled_s + 0.5 < float(gate.duration_s)
    ):
        reasons.append(f"sampled {sampled_s:.1f}s, gate requires {gate.duration_s}s")
    if extra_reasons:
        reasons.extend(extra_reasons)
    return {
        "name": gate.name,
        "status": "measured",
        "passed": not reasons,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "error_rate": rate,
        "reasons": reasons,
        "gate": {
            "p95_ms": gate.p95_ms,
            "error_rate": gate.error_rate,
            "max_loss": gate.max_loss,
            "max_s": gate.max_s,
        },
    }


def not_run(gate: Gate, why: str) -> dict[str, Any]:
    return {
        "name": gate.name,
        "status": "not_run",
        "passed": False,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "error_rate": None,
        "reasons": [why],
        "gate": {
            "p95_ms": gate.p95_ms,
            "error_rate": gate.error_rate,
            "max_loss": gate.max_loss,
            "max_s": gate.max_s,
        },
    }


def report(
    *,
    shape: Shape,
    sha: str,
    cpu_percent: float,
    rss_mib: float,
    gates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "git_sha": sha,
        "shape": {"os": shape.os, "vcpu": shape.vcpu, "ram_gib": shape.ram_gib},
        "reference_shape": {"os": "linux", "vcpu": REFERENCE_VCPU, "ram_gib": REFERENCE_RAM_GIB},
        "shape_ok": matches_reference(shape),
        "deterministic_delay_ms": DETERMINISTIC_DELAY_MS,
        "preload_run_events": PRELOAD_RUN_EVENTS,
        "cpu_percent": cpu_percent,
        "rss_mib": rss_mib,
        "gates": gates,
        "passed": bool(
            matches_reference(shape)
            and gates
            and all(item["passed"] for item in gates.values())
        ),
    }


def measured_fail(gate: Gate, why: str) -> dict[str, Any]:
    return {
        "name": gate.name,
        "status": "measured",
        "passed": False,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "error_rate": None,
        "reasons": [why],
        "gate": {
            "p95_ms": gate.p95_ms,
            "error_rate": gate.error_rate,
            "max_loss": gate.max_loss,
            "max_s": gate.max_s,
        },
    }


_live_module: Any = None


def drive_gate(gate: Gate, seconds: int | None) -> dict[str, Any]:
    """Run one live driver. Tests replace this; the default loads benchmark_live."""
    global _live_module
    if _live_module is None:
        path = Path(__file__).resolve().with_name("benchmark_live.py")
        spec = spec_from_file_location("tiny_hermes_benchmark_live", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        module = module_from_spec(spec)
        # dataclasses look up sys.modules[cls.__module__] while the class body
        # runs; leaving the name unset is AttributeError on None.__dict__.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _live_module = module
    return _live_module.run_driver(gate, seconds, evaluate=evaluate, sample_type=Sample)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape-only", action="store_true")
    parser.add_argument("--gate", action="append", dest="only")
    parser.add_argument(
        "--seconds",
        type=int,
        default=None,
        help="Cap duration-gate sampling. A shorter sample cannot pass §24.1.",
    )
    args = parser.parse_args(argv)
    shape = host_shape()
    sha = git_sha()
    usage = process_usage()
    if args.shape_only:
        document = report(
            shape=shape,
            sha=sha,
            cpu_percent=usage.cpu_percent,
            rss_mib=usage.rss_mib,
            gates={},
        )
        print(json.dumps(document, indent=2))
        if not document["shape_ok"]:
            raise SystemExit(2)
        return
    selected = [gate for gate in GATES if args.only is None or gate.name in args.only]
    ready = api_ready()
    if not ready:
        why = (
            "API /health/ready is not 200; live gates need Compose on the Linux "
            "reference shape (8 vCPU, 16 GiB, 50 ms deterministic delay, 100k events)"
        )
        gates = {gate.name: not_run(gate, why) for gate in selected}
    else:
        gates = {}
        for gate in selected:
            try:
                gates[gate.name] = drive_gate(gate, args.seconds)
            except Exception as error:  # noqa: BLE001 - a driver crash is a failed gate
                gates[gate.name] = measured_fail(gate, f"{type(error).__name__}: {error}")
    document = report(
        shape=shape,
        sha=sha,
        cpu_percent=usage.cpu_percent,
        rss_mib=usage.rss_mib,
        gates=gates,
    )
    print(json.dumps(document, indent=2))
    if not document["shape_ok"]:
        raise SystemExit(2)
    if not ready:
        raise SystemExit(3)
    if not document["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
