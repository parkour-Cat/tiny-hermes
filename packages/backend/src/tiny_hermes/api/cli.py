import asyncio
import contextlib
import logging
import signal
import socket
import uuid

import uvicorn

from tiny_hermes.runs.application.scheduler import (
    SchedulerRuntime,
    SchedulerSettings,
)
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.database import build_session_factory
from tiny_hermes.shared.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    uvicorn.run(
        "tiny_hermes.api.app:app",
        host="0.0.0.0",  # noqa: S104 - container port must accept traffic outside itself
        port=8000,
    )


def worker_main() -> None:
    configure_logging()
    asyncio.run(_worker())


async def _worker() -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    notifier = _notifier(settings)
    runtime = WorkerRuntime(
        session_factory=build_session_factory(settings),
        model=DeterministicModelProvider(settings.deterministic_model_delay_ms),
        notifier=notifier,
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
            max_slice_seconds=settings.worker_max_slice_seconds,
            idle_poll_seconds=settings.worker_idle_poll_seconds,
        ),
    )
    stop = _stop_on_termination()
    logger.info("worker started", extra={"worker_id": worker_id})
    try:
        await runtime.run_forever(stop)
    finally:
        await notifier.close()
    logger.info("worker stopped", extra={"worker_id": worker_id})


def scheduler_main() -> None:
    configure_logging()
    asyncio.run(_scheduler())


async def _scheduler() -> None:
    settings = get_settings()
    notifier = _notifier(settings)
    runtime = SchedulerRuntime(
        session_factory=build_session_factory(settings),
        notifier=notifier,
        settings=SchedulerSettings(
            max_recovery_attempts=settings.max_recovery_attempts,
            event_retention_hours=settings.event_retention_hours,
        ),
    )
    stop = _stop_on_termination()
    logger.info("scheduler started")
    try:
        await runtime.run_forever(stop, settings.scheduler_interval_seconds)
    finally:
        await notifier.close()
    logger.info("scheduler stopped")


def _notifier(settings: Settings) -> WakeUpNotifier:
    url = settings.redis_url
    return RedisWakeUpNotifier(url) if url else NullWakeUpNotifier()


def _stop_on_termination() -> asyncio.Event:
    stop = asyncio.Event()
    running = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        with contextlib.suppress(NotImplementedError):
            running.add_signal_handler(received, stop.set)
    return stop
