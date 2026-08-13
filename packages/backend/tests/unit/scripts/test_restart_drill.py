"""The restart drill must not turn Docker errors into reassuring results."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_SPEC = spec_from_file_location(
    "tiny_hermes_restart_drill",
    Path(__file__).parents[5] / "scripts" / "restart_drill.py",
)
assert _SPEC is not None and _SPEC.loader is not None
restart_drill = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = restart_drill
_SPEC.loader.exec_module(restart_drill)


def test_container_listing_reports_a_docker_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(
        command: Sequence[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="daemon denied")

    monkeypatch.setattr(restart_drill.subprocess, "run", refused)

    with pytest.raises(SystemExit, match="docker ps failed.*daemon denied"):
        restart_drill.containers_for_drill()


def test_command_observer_requests_pid_and_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def docker(
        command: Sequence[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        call = list(command)
        calls.append(call)
        if call[1] == "ps":
            return subprocess.CompletedProcess(call, 0, stdout="sandbox-id\n", stderr="")
        if call[-2:] == ["-eo", "pid,args"]:
            return subprocess.CompletedProcess(
                call,
                0,
                stdout="PID COMMAND\n123 sleep 20\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            call,
            1,
            stdout="",
            stderr="Couldn't find PID field in ps output",
        )

    monkeypatch.setattr(restart_drill.subprocess, "run", docker)

    assert restart_drill.await_sandbox_command(set(), timeout=0.01) == {"sandbox-id"}
    assert ["docker", "top", "sandbox-id", "-eo", "pid,args"] in calls


def test_fourth_scenario_refuses_to_skip_without_an_approved_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_IMAGE_DIGEST", raising=False)

    with pytest.raises(SystemExit, match="approved sandbox image"):
        restart_drill.sandbox_leak(object(), "workspace-id")


def test_await_status_fails_fast_when_the_run_already_ended_elsewhere() -> None:
    """A failed Run must not burn the drill's timeout waiting for completed.

    compose-e2e did exactly that: the large commit was already `failed`, and
    `await_status(..., ['completed'], 300)` polled for five more minutes.
    """

    class _Answer:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"status": "failed"}

    class _Client:
        def get(self, *_: object, **__: object) -> _Answer:
            return _Answer()

    console = restart_drill.Console(_Client())  # type: ignore[arg-type]
    started = time.monotonic()
    with pytest.raises(SystemExit, match="ended as 'failed'"):
        console.await_status("workspace", "run-id", ["completed"], 30.0)
    assert time.monotonic() - started < 1.0
