"""The controller process sweeps its host on a timer, and a failed pass is
not the end of it."""

import asyncio
import contextlib

from tiny_hermes.sandbox.application.controller import SweepResult
from tiny_hermes.sandbox.cli import sweep_forever

from .test_controller_wiring import _settings  # pyright: ignore[reportPrivateUsage]


async def test_the_first_pass_runs_at_startup_and_a_failure_does_not_stop_the_next() -> None:
    """At startup, because a controller that just restarted is most likely
    restarting after the crash that left something behind."""
    passes: list[int] = []

    async def once() -> SweepResult:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("daemon unreachable")
        return SweepResult(containers=1, volumes=0)

    task = asyncio.create_task(sweep_forever(once, every=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(passes) >= 3


def test_the_sweep_runs_every_five_minutes_unless_told_otherwise() -> None:
    settings = _settings()
    assert settings.sandbox_sweep_seconds == 300
    assert _settings(sandbox_sweep_seconds=10).sandbox_sweep_seconds == 10
