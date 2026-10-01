"""The concurrency probe's arithmetic: what it reports must follow from the Run rows."""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

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
probe = _load("tiny_hermes_concurrency_probe", _SCRIPTS / "concurrency_probe.py")


def run(
    run_id: str,
    created: float,
    started: float | None,
    finished: float | None,
    status: str = "completed",
) -> Any:
    return probe.RunTimes(
        run_id=run_id,
        status=status,
        created_at=created,
        started_at=started,
        finished_at=finished,
    )


def test_arrivals_are_evenly_spaced_and_stop_before_the_window_ends() -> None:
    assert probe.arrival_offsets(2.0, 3.0) == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]


def test_a_rate_that_submits_nothing_is_refused() -> None:
    with pytest.raises(ValueError):
        probe.arrival_offsets(0.0, 10.0)


def test_peak_overlap_counts_runs_executing_at_the_same_instant() -> None:
    assert probe.peak_overlap([(0.0, 4.0), (1.0, 5.0), (2.0, 3.0)]) == 3


def test_a_run_that_starts_as_another_finishes_does_not_overlap_it() -> None:
    # One Worker hands over at t=3: that is a serial queue, not two at once.
    assert probe.peak_overlap([(0.0, 3.0), (3.0, 6.0)]) == 1


def test_peak_overlap_of_nothing_is_zero() -> None:
    assert probe.peak_overlap([]) == 0


def test_one_worker_serializes_runs_and_the_wait_grows() -> None:
    # Three Runs a second apart, each needing 3s, one Worker: the second waits
    # 2s and the third 4s. This is the shape the default stack should show.
    runs = [
        run("a", 0.0, 0.0, 3.0),
        run("b", 1.0, 3.0, 6.0),
        run("c", 2.0, 6.0, 9.0),
    ]

    summary = probe.summarize(runs, observed_until=9.0)

    assert summary["peak_running"] == 1
    assert summary["queue_wait_s"]["max"] == pytest.approx(4.0)
    assert summary["queue_wait_s"]["p50"] == pytest.approx(2.0)
    assert summary["service_s"]["p50"] == pytest.approx(3.0)
    assert summary["end_to_end_s"]["max"] == pytest.approx(7.0)
    assert summary["throughput_per_s"] == pytest.approx(3 / 9)
    assert summary["completed"] == 3


def test_a_run_that_never_started_still_counts_its_wait_as_a_lower_bound() -> None:
    # Dropping it would report the Runs that got through and hide the one that
    # did not, which is the exact failure a saturation probe exists to show.
    runs = [
        run("a", 0.0, 0.0, 2.0),
        run("b", 1.0, None, None, status="queued"),
    ]

    summary = probe.summarize(runs, observed_until=11.0)

    assert summary["never_started"] == 1
    assert summary["unfinished"] == 1
    assert summary["queue_wait_s"]["max"] == pytest.approx(10.0)
    assert summary["queue_wait_censored"] == 1


def test_a_run_still_executing_at_the_end_is_running_until_then() -> None:
    runs = [
        run("a", 0.0, 0.0, None, status="running"),
        run("b", 0.0, 1.0, 2.0),
    ]

    summary = probe.summarize(runs, observed_until=5.0)

    assert summary["peak_running"] == 2
    assert summary["unfinished"] == 1
    # Service time is only reported for Runs that finished.
    assert summary["service_s"]["max"] == pytest.approx(1.0)


def test_failed_runs_are_counted_and_kept_out_of_the_service_time() -> None:
    runs = [
        run("a", 0.0, 0.0, 3.0),
        run("b", 0.0, 0.0, 0.5, status="failed"),
    ]

    summary = probe.summarize(runs, observed_until=3.0)

    assert summary["failed"] == 1
    assert summary["completed"] == 1
    assert summary["service_s"]["max"] == pytest.approx(3.0)
    assert summary["status_counts"] == {"completed": 1, "failed": 1}


def test_mean_running_is_busy_time_over_the_window() -> None:
    # Little's law read backwards: 2 Runs x 3s busy in a 6s window is an
    # average of 1 Run executing — the number a Worker count has to cover.
    runs = [run("a", 0.0, 0.0, 3.0), run("b", 3.0, 3.0, 6.0)]

    summary = probe.summarize(runs, observed_until=6.0)

    assert summary["mean_running"] == pytest.approx(1.0)


def test_nothing_submitted_summarizes_to_zeros_rather_than_raising() -> None:
    summary = probe.summarize([], observed_until=0.0)

    assert summary["submitted"] == 0
    assert summary["throughput_per_s"] == 0.0
    assert summary["peak_running"] == 0


def test_docker_memory_strings_become_mebibytes() -> None:
    assert probe.to_mib("512MiB") == pytest.approx(512.0)
    assert probe.to_mib("1.5GiB") == pytest.approx(1536.0)
    assert probe.to_mib("2048KiB") == pytest.approx(2.0)


def test_an_integer_setting_is_read_from_a_container_environment() -> None:
    # `docker inspect` prints one KEY=value per line. A container count alone
    # stopped describing the stack once one container could hold K Workers.
    env = "PATH=/usr/bin\nWORKER_CONCURRENCY=8\nDETERMINISTIC_MODEL_DELAY_MS=3000\n"

    assert probe.env_int(env, "WORKER_CONCURRENCY") == 8
    assert probe.env_int(env, "DETERMINISTIC_MODEL_DELAY_MS") == 3000


def test_a_setting_that_is_absent_or_not_a_number_reads_as_unknown() -> None:
    assert probe.env_int("PATH=/usr/bin\n", "WORKER_CONCURRENCY") is None
    assert probe.env_int("WORKER_CONCURRENCY=eight\n", "WORKER_CONCURRENCY") is None


def test_runs_split_into_the_groups_the_probe_submitted() -> None:
    runs = [run("long-1", 0.0, 0.0, 9.0), run("short-1", 1.0, 1.0, 2.0), run("x", 1.0, 1.0, 2.0)]

    long_runs, short_runs = probe.split_runs(runs, {"long-1"}, {"short-1"})

    assert [r.run_id for r in long_runs] == ["long-1"]
    assert [r.run_id for r in short_runs] == ["short-1"]


def test_sandbox_containers_are_counted_by_state() -> None:
    # `docker ps --format {{.State}}`: a frozen sandbox is a paused container.
    assert probe.count_states("running\npaused\nrunning\nexited\n") == {
        "running": 2,
        "paused": 1,
        "exited": 1,
    }
    assert probe.count_states("") == {}


def test_a_container_that_vanishes_mid_sample_does_not_stop_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A sandbox destroyed between `docker ps` and `docker stats` makes the
    # second call fail. That is the stack working, not the probe breaking: on
    # 2026-09-29 it killed the sampling thread and the sandbox counts stopped.
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str) -> str:
        calls.append(arguments)
        if arguments[0] == "stats":
            raise probe.subprocess.CalledProcessError(1, ["docker", *arguments])
        return "running\n" if "{{.State}}" in arguments else "gone-already\n"

    monkeypatch.setattr(probe, "_docker", docker)
    sampler = probe._Sampler("workspace", [])  # pyright: ignore[reportPrivateUsage]

    sampler._sample_sandboxes()  # pyright: ignore[reportPrivateUsage]

    assert sampler.max_sandboxes == {"running": 1}
    assert any(call[0] == "stats" for call in calls)


def test_a_long_task_asks_its_sandbox_to_hold_memory_only_when_told_to() -> None:
    assert probe.long_task_input(12, cache_mb=0) == "rounds=12"
    assert probe.long_task_input(12, cache_mb=100) == "rounds=12 cache=100"


def test_sandboxes_alive_at_once_are_counted_apart_from_the_peak_of_each_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The per-state peaks come from different moments. On 2026-09-30 "running
    # 32, paused 31" was read as 63 sandboxes at once; there were never more
    # than 32.
    samples = iter(["running\nrunning\npaused\n", "running\npaused\npaused\n"])

    def docker(*arguments: str) -> str:
        return next(samples) if "{{.State}}" in arguments else ""

    monkeypatch.setattr(probe, "_docker", docker)
    sampler = probe._Sampler("workspace", [])  # pyright: ignore[reportPrivateUsage]

    sampler._sample_sandboxes()  # pyright: ignore[reportPrivateUsage]
    sampler._sample_sandboxes()  # pyright: ignore[reportPrivateUsage]

    assert sampler.max_sandboxes == {"running": 2, "paused": 2}
    assert sampler.max_sandboxes_at_once == 3
