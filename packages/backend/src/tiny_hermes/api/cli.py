import asyncio
import contextlib
import logging
import signal
import socket
import uuid

import uvicorn

from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.shared.config import get_settings
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
    runtime = WorkerRuntime(
        session_factory=build_session_factory(settings),
        model=DeterministicModelProvider(settings.deterministic_model_delay_ms),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
            max_slice_seconds=settings.worker_max_slice_seconds,
            idle_poll_seconds=settings.worker_idle_poll_seconds,
        ),
    )
    stop = _stop_on_termination()
    logger.info("worker started", extra={"worker_id": worker_id})
    await runtime.run_forever(stop)
    logger.info("worker stopped", extra={"worker_id": worker_id})


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
