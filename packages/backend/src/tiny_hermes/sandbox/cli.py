"""The Sandbox Controller as a process of its own.

It exists as a separate service for one reason: it is the only thing that mounts
the Docker socket. Product design §16 — `Docker 控制权只授予可信的
sandbox-controller。API、Web、Agent 沙箱和模型均不得直接访问 Docker socket`. A
Worker that wanted to create a privileged container has no socket to ask, and
that is a property of the Compose file rather than of anybody's discipline.

This module is the only place a Docker client is constructed, which the phase-3B
ban enforces.
"""

import asyncio
import json
import logging
import signal
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

import docker
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.sandbox.application.controller import (
    DATA_MOUNT,
    AuditEntry,
    SandboxController,
    SandboxRefused,
)
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import DEFAULT_PROFILE
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.infrastructure.lease_authority import SqlLeaseAuthority
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.sandbox.transport.server import ControllerServer, ServerStream
from tiny_hermes.shared.config import get_settings
from tiny_hermes.shared.database import build_session_factory
from tiny_hermes.shared.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    asyncio.run(_serve())


async def _serve() -> None:
    settings = get_settings()
    sessions = build_session_factory(settings)
    # The operator's cache ceiling, threaded once at startup: with M1's single
    # profile, "default" is whatever the instance configuration says it is.
    ceiling = replace(
        DEFAULT_PROFILE,
        cache_mb=settings.sandbox_cache_mb,
        cache_inodes=settings.sandbox_cache_inodes,
    )
    client: Any = docker.from_env()  # noqa: TID251 - the one place, by design

    async def dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One session per call.

        A Controller holding one long-lived session would keep a transaction
        open across the seconds a container start takes, and a Worker's slice
        would then wait on a lock the Controller forgot it held.
        """
        async with sessions() as session:
            controller = SandboxController(
                engine=DockerEngine(client),
                store=SqlSandboxStore(session),
                approved_digests=settings.approved_image_digests,
                leases=SqlLeaseAuthority(session),
                audit=_AuditSink(session),
                ceiling=ceiling,
            )
            try:
                answer = await _invoke(controller, action, payload)
            except SandboxRefused:
                # A refusal is a decision, and decisions that were audited on
                # the way to being refused should survive.
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return answer

    async def dispatch_stream(
        action: str, payload: dict[str, Any], channel: ServerStream
    ) -> dict[str, Any]:
        async with sessions() as session:
            controller = SandboxController(
                engine=DockerEngine(client),
                store=SqlSandboxStore(session),
                approved_digests=settings.approved_image_digests,
                leases=SqlLeaseAuthority(session),
                audit=_AuditSink(session),
                ceiling=ceiling,
            )
            try:
                answer = await _invoke_stream(controller, action, payload, channel, settings)
            except SandboxRefused:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return answer

    server = ControllerServer(
        dispatch=dispatch,
        stream_dispatch=dispatch_stream,
        path=settings.sandbox_controller_socket,
        group=settings.sandbox_socket_gid,
    )
    await server.start()
    logger.info(
        "sandbox controller started",
        extra={"socket": settings.sandbox_controller_socket},
    )
    stop = _stop_on_termination()
    try:
        await stop
    finally:
        await server.stop()
        client.close()
    logger.info("sandbox controller stopped")


async def _invoke(
    controller: SandboxController, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Decoding, and nothing else.

    The server refused any action outside the vocabulary before this was
    reached, so there is no default branch to get wrong.
    """
    run_id = UUID(str(payload["run_id"]))
    if action == "acquire":
        raw_session = payload.get("session_id")
        answer = await controller.acquire(
            run_id=run_id,
            lease_id=UUID(str(payload["lease_id"])),
            workspace_id=UUID(str(payload["workspace_id"])),
            session_id=None if raw_session is None else UUID(str(raw_session)),
            profile=str(payload["profile"]),
        )
        return {"sandbox_id": str(answer.sandbox_id), "cache_state": answer.cache_state.value}

    sandbox_id = UUID(str(payload["sandbox_id"]))
    if action == "cleanup":
        await controller.cleanup(run_id=run_id, sandbox_id=sandbox_id)
        return {}
    if action == "volume_remove":
        await controller.volume_remove(run_id=run_id, sandbox_id=sandbox_id)
        return {}
    if action == "inspect":
        instance = await controller.inspect(run_id=run_id, sandbox_id=sandbox_id)
        return {"status": instance.status.value, "boot_id": instance.boot_id}
    if action == "keep":
        await controller.keep(
            run_id=run_id,
            sandbox_id=sandbox_id,
            until=datetime.fromisoformat(str(payload["until"])),
        )
        return {}

    lease_id = UUID(str(payload["lease_id"]))
    if action == "execute":
        request: Any = payload["command"]
        result = await controller.execute(
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            command=SandboxCommand(
                argv=[str(part) for part in request["argv"]],
                cwd=str(request["cwd"]),
                timeout_seconds=int(request["timeout_seconds"]),
                output_limit=int(request["output_limit"]),
            ),
        )
        return {
            "exit_code": result.exit_code,
            "output": result.output,
            "truncated": result.truncated,
            "timed_out": result.timed_out,
        }
    if action == "freeze":
        await controller.freeze(run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id)
        return {}
    if action == "thaw":
        await controller.thaw(run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id)
        return {}
    await controller.destroy(run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id)
    return {}


async def _invoke_stream(
    controller: SandboxController,
    action: str,
    payload: dict[str, Any],
    channel: ServerStream,
    settings: Any,
) -> dict[str, Any]:
    """Streaming actions: authorize through the Controller, then move bytes.

    The Controller's ticket is what separates "may" from "does": no frame is
    read or written until the same checks `execute` makes have passed, and the
    container id the engine receives came from the reservation, never from the
    payload.
    """
    run_id = UUID(str(payload["run_id"]))
    lease_id = UUID(str(payload["lease_id"]))
    sandbox_id = UUID(str(payload["sandbox_id"]))

    if action == "workspace_import":
        ticket = await controller.workspace_import(
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            declared_total=int(payload["declared_total"]),
        )
        try:
            await controller.engine.import_tree(
                ticket.container_id,
                DATA_MOUNT,
                channel.receive(declared_limit=int(settings.workspace_max_bytes)),
            )
        except BaseException:
            await controller.workspace_import_failed(run_id=run_id, sandbox_id=sandbox_id)
            raise
        return {"received_bytes": channel.received_bytes}

    if action == "workspace_scan":
        # Full-tree metadata is workspace-sized, not line-sized: a hundred
        # thousand entries is megabytes of JSON, so it travels as frames.
        entries = await controller.workspace_scan(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id
        )
        document = json.dumps(
            {
                "entries": [
                    {
                        "path": entry.path,
                        "type": entry.entry_type,
                        "mode": entry.mode,
                        "size": entry.size,
                        "sha256": entry.sha256,
                    }
                    for entry in entries
                ]
            }
        ).encode()
        await channel.start_send(total_limit=len(document))
        await channel.push(document)
        return await channel.finish_send()

    if action == "workspace_export":
        ticket = await controller.workspace_export(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id
        )
        # Tar framing adds headers and padding on top of the quota's bytes.
        cap = int(settings.workspace_max_bytes) + int(settings.workspace_max_objects) * 2048
        await channel.start_send(total_limit=cap)
        async for chunk in controller.engine.export_tree(ticket.container_id, DATA_MOUNT):
            await channel.push(chunk)
        return await channel.finish_send()

    request: Any = payload["command"]
    command = SandboxCommand(
        argv=[str(part) for part in request["argv"]],
        cwd=str(request["cwd"]),
        timeout_seconds=int(request["timeout_seconds"]),
        output_limit=int(request["output_limit"]),
    )
    if action == "execute_stdin":
        # The same authorization execute makes; the body arrives as frames
        # because the line protocol's size cap is a guard worth keeping.
        ticket = await controller.execute_stream(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id, command=command
        )
        received = bytearray()
        async for chunk in channel.receive(
            declared_limit=int(settings.workspace_file_write_bytes)
        ):
            received.extend(chunk)
        outcome = await controller.engine.execute(
            ticket.container_id, replace(command, stdin=bytes(received))
        )
        return {
            "exit_code": outcome.exit_code,
            "output": outcome.output,
            "truncated": outcome.truncated,
            "timed_out": outcome.timed_out,
        }
    ticket = await controller.execute_stream(
        run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id, command=command
    )
    cap = min(int(payload["artifact_limit"]), int(settings.artifact_max_bytes))
    await channel.start_send(total_limit=cap)
    sink = _ChannelSink(channel, artifact_limit=cap)
    result = await controller.engine.execute_streamed(
        ticket.container_id, command, sink
    )
    await channel.finish_send()
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "observed_bytes": result.observed_bytes,
        "delivered_bytes": result.delivered_bytes,
        "truncated": result.truncated,
    }


class _ChannelSink:
    """The engine's OutputSink, writing frames as bytes arrive."""

    def __init__(self, channel: ServerStream, *, artifact_limit: int) -> None:
        self._channel = channel
        self.artifact_limit = artifact_limit

    async def deliver(self, chunk: bytes) -> None:
        await self._channel.push(chunk)


class _AuditSink:
    """The Scheduler's cleanup authority, written down.

    `actor_type` is the platform rather than a user, because no user asked for
    this — a Run outlived its lease and something had to reclaim the container.
    An operator reading the trail later needs to know that.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, entry: AuditEntry) -> None:
        self._session.add(
            AuditEventRow(
                workspace_id=None,
                actor_type="platform",
                actor_id=None,
                action=entry.action,
                resource_type="sandbox_instance",
                resource_id=entry.sandbox_id,
                result="succeeded",
                request_id=str(entry.run_id),
                context={"detail": entry.detail, "run_id": str(entry.run_id)},
            )
        )


def _stop_on_termination() -> asyncio.Future[None]:
    loop = asyncio.get_running_loop()
    stop: asyncio.Future[None] = loop.create_future()

    def finish() -> None:
        if not stop.done():
            stop.set_result(None)

    for name in (signal.SIGTERM, signal.SIGINT):
        with _ignoring_unsupported():
            loop.add_signal_handler(name, finish)
    return stop


class _ignoring_unsupported:  # noqa: N801 - a context manager used as a statement
    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: object, error: object, traceback: object) -> bool:
        return isinstance(error, NotImplementedError)
