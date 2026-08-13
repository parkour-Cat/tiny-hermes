"""The benchmark's gates are the product table. Editing them to pass is the bug."""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = spec_from_file_location(
    "tiny_hermes_benchmark_m1",
    Path(__file__).parents[5] / "scripts" / "benchmark_m1.py",
)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = benchmark
_SPEC.loader.exec_module(benchmark)


def test_thresholds_are_the_product_24_1_cells() -> None:
    assert benchmark.CREATE_RUN.sessions == 200
    assert benchmark.CREATE_RUN.rps == 20
    assert benchmark.CREATE_RUN.duration_s == 300
    assert benchmark.CREATE_RUN.p95_ms == 300.0
    assert benchmark.CREATE_RUN.error_rate == 0.001
    assert benchmark.RUN_EVENT.rps == 500
    assert benchmark.RUN_EVENT.duration_s == 300
    assert benchmark.RUN_EVENT.p95_ms == 100.0
    assert benchmark.RUN_EVENT.max_loss == 0
    assert benchmark.SSE.connections == 500
    assert benchmark.SSE.duration_s == 600
    assert benchmark.SANDBOX_COLD.p95_ms == 3000.0
    assert benchmark.SANDBOX_WARM.p95_ms == 300.0
    assert benchmark.WORKSPACE_SMALL.p95_ms == 1000.0
    assert benchmark.WORKSPACE_LARGE.p95_ms == 15000.0
    assert benchmark.NEXT_RUN.p95_ms == 3000.0
    assert benchmark.WORKER_RECOVERY.max_s == 30.0
    assert benchmark.SERVICE_RECOVERY.max_s == 60.0
    assert benchmark.PRELOAD_RUN_EVENTS == 100_000
    assert benchmark.DETERMINISTIC_DELAY_MS == 50
    assert benchmark.REFERENCE_VCPU == 8
    assert benchmark.REFERENCE_RAM_GIB == 16


def test_percentile_is_stable_at_the_edges() -> None:
    assert benchmark.percentile((), 95.0) == 0.0
    assert benchmark.percentile((2.0,), 95.0) == 2.0
    climbing = tuple(float(value) for value in range(1, 21))
    assert benchmark.percentile(climbing, 50.0) == pytest.approx(10.5)
    assert benchmark.percentile(climbing, 95.0) >= 19.0


def test_evaluate_fails_when_p95_exceeds_the_gate() -> None:
    sample = benchmark.Sample(latencies_ms=(400.0,) * 20, errors=0, total=20)
    result = benchmark.evaluate(benchmark.CREATE_RUN, sample)
    assert result["passed"] is False
    assert result["p95_ms"] == pytest.approx(400.0)


def test_evaluate_fails_when_the_error_rate_exceeds_the_gate() -> None:
    sample = benchmark.Sample(latencies_ms=(10.0,) * 100, errors=1, total=100)
    result = benchmark.evaluate(benchmark.CREATE_RUN, sample)
    assert result["passed"] is False


def test_evaluate_passes_a_sample_inside_the_gate() -> None:
    sample = benchmark.Sample(latencies_ms=(50.0,) * 100, errors=0, total=100)
    result = benchmark.evaluate(benchmark.CREATE_RUN, sample)
    assert result["passed"] is True
    assert {"p50_ms", "p95_ms", "p99_ms", "error_rate"} <= result.keys()


def test_a_4_vcpu_host_is_not_the_reference_shape() -> None:
    shape = benchmark.Shape(os="linux", vcpu=4, ram_gib=16.0)
    assert benchmark.matches_reference(shape) is False


def test_linux_8_vcpu_16_gib_is_the_reference_shape() -> None:
    shape = benchmark.Shape(os="linux", vcpu=8, ram_gib=16.0)
    assert benchmark.matches_reference(shape) is True


def test_marketed_16_gib_is_not_rejected_for_kernel_pages() -> None:
    shape = benchmark.Shape(os="linux", vcpu=8, ram_gib=15.64)
    assert benchmark.matches_reference(shape) is True


def test_shape_only_exits_nonzero_when_the_host_is_too_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "host_shape",
        lambda: benchmark.Shape(os="linux", vcpu=4, ram_gib=16.0),
    )
    with pytest.raises(SystemExit) as refused:
        benchmark.main(["--shape-only"])
    assert refused.value.code == 2


def test_a_report_names_sha_cpu_rss_and_never_lowers_a_gate() -> None:
    source = Path(__file__).parents[5] / "scripts" / "benchmark_m1.py"
    text = source.read_text(encoding="utf-8")
    assert "p95_ms=300.0" in text or "p95_ms=300" in text
    assert "error_rate=0.001" in text
    assert "allow_skip" not in text
    assert "relax" not in text.lower()
    document = benchmark.report(
        shape=benchmark.Shape(os="linux", vcpu=4, ram_gib=16.0),
        sha="abc123",
        cpu_percent=12.0,
        rss_mib=256.0,
        gates={"create_run": {"passed": False, "status": "not_run"}},
    )
    assert document["git_sha"] == "abc123"
    assert document["cpu_percent"] == 12.0
    assert document["rss_mib"] == 256.0
    assert document["passed"] is False


def test_gates_are_frozen_and_cannot_be_assigned() -> None:
    with pytest.raises(AttributeError):
        benchmark.CREATE_RUN.p95_ms = 10_000  # type: ignore[misc]


def test_main_does_not_report_pass_when_the_api_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "host_shape",
        lambda: benchmark.Shape(os="linux", vcpu=8, ram_gib=16.0),
    )
    monkeypatch.setattr(benchmark, "git_sha", lambda: "deadbeef")
    monkeypatch.setattr(benchmark, "api_ready", lambda: False)
    monkeypatch.setattr(
        benchmark,
        "process_usage",
        lambda: SimpleNamespace(cpu_percent=1.0, rss_mib=10.0),
    )
    with pytest.raises(SystemExit) as refused:
        benchmark.main([])
    assert refused.value.code != 0
