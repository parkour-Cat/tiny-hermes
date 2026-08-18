import asyncio
import contextlib
import logging
import signal
import socket
import uuid

import uvicorn

from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.runs.application.model_router import ModelRouter
from tiny_hermes.runs.application.scheduler import (
    SchedulerRuntime,
    SchedulerSettings,
)
from tiny_hermes.runs.application.worker import (
    WorkerRuntime,
    WorkerSettings,
    WorkspaceRuntime,
)
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.openai_model import RetryPolicy
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier
from tiny_hermes.runs.infrastructure.skill_library import SqlSkillLibrary
from tiny_hermes.runs.infrastructure.skill_proposals import SqlSkillProposals
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.sandbox.transport.adapter import SandboxClient
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.secrets.domain.envelope import optional_kek
from tiny_hermes.session_workspace.domain.models import WorkspaceQuota
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
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
    sessions = build_session_factory(settings)
    workspace = _workspace(settings)
    await _ensure_bucket(workspace)
    if workspace is not None:
        logger.info(
            "workspace runtime configured: quota_bytes=%s", workspace.quota.max_bytes
        )
    # One provider port, two providers behind it. Which one answers is decided
    # per round by the Agent Version the Run fixed at creation, so the Worker
    # never learns that endpoints exist.
    runtime = WorkerRuntime(
        session_factory=sessions,
        model=ModelRouter(
            deterministic=DeterministicModelProvider(settings.deterministic_model_delay_ms),
            session_factory=sessions,
            client_factory=lambda: SafeOutboundClient(
                approved=settings.approved_networks,
                connect_timeout=settings.outbound_connect_timeout_seconds,
                read_timeout=settings.outbound_read_timeout_seconds,
                max_redirects=settings.outbound_max_redirects,
                max_response_bytes=settings.outbound_max_response_bytes,
            ),
            retry=RetryPolicy(
                max_attempts=settings.model_max_attempts,
                base_ms=settings.model_retry_base_ms,
            ),
            kek=optional_kek(settings.tiny_hermes_kek),
        ),
        notifier=notifier,
        # Absent when no image is approved: a deployment that has not chosen
        # one cannot run a tool, and a Run that binds one fails rather than
        # running the command anywhere else.
        sandbox=_controller(settings),
        workspace=workspace,
        # Skill text is read from the catalog the same database holds, so a
        # deployment configures nothing extra to give an Agent skills.
        skills=SqlSkillLibrary(sessions),
        # The Agent's half of §15.3. It writes proposals and can approve
        # none of them, which is the whole governance story in one field.
        proposals=SqlSkillProposals(sessions),
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
            max_slice_seconds=settings.worker_max_slice_seconds,
            idle_poll_seconds=settings.worker_idle_poll_seconds,
            sandbox_idle_ttl_seconds=settings.sandbox_idle_ttl_seconds,
        ),
    )
    stop = _stop_on_termination()
    logger.info("worker started", extra={"worker_id": worker_id})
    try:
        await runtime.run_forever(stop)
    finally:
        await notifier.close()
    logger.info("worker stopped", extra={"worker_id": worker_id})


def _controller(settings: Settings) -> SandboxClient | None:
    """The Controller over its socket, when this deployment has one."""
    if not settings.sandbox_image_digest:
        return None
    return SandboxClient(ControllerClient(settings.sandbox_controller_socket))


async def _ensure_bucket(workspace: WorkspaceRuntime | None) -> None:
    """Idempotent bucket creation at boot, so the first checkpoint is not the
    request that discovers a fresh MinIO. A failure here is logged rather than
    fatal: the store may simply not be up yet, and every later operation
    reports `workspace_storage_unavailable` honestly."""
    if workspace is None:
        return
    ensure = getattr(workspace.objects, "ensure_bucket", None)
    if ensure is None:
        return
    try:
        await ensure()
    except Exception:
        logger.exception("object-store bucket check failed at startup")


def _workspace(settings: Settings) -> WorkspaceRuntime | None:
    """Persistent session files, when this deployment runs sandboxes at all."""
    if not settings.sandbox_image_digest:
        return None
    return WorkspaceRuntime(
        objects=MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        quota=WorkspaceQuota(
            max_bytes=settings.workspace_max_bytes,
            max_objects=settings.workspace_max_objects,
        ),
        staging_ttl_seconds=settings.workspace_staging_ttl_seconds,
        # Tar framing adds headers and padding on top of the quota's bytes.
        export_limit=settings.workspace_max_bytes + settings.workspace_max_objects * 2048,
        artifact_max_bytes=settings.artifact_max_bytes,
        run_artifact_max_bytes=settings.run_artifact_max_bytes,
        preview_bytes=settings.shell_output_bytes,
    )


def scheduler_main() -> None:
    configure_logging()
    asyncio.run(_scheduler())


async def _scheduler() -> None:
    settings = get_settings()
    notifier = _notifier(settings)
    workspace = _workspace(settings)
    await _ensure_bucket(workspace)
    runtime = SchedulerRuntime(
        session_factory=build_session_factory(settings),
        notifier=notifier,
        sandbox=_controller(settings),
        objects=None if workspace is None else workspace.objects,
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
