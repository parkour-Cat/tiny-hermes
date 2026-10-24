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
import contextlib
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import docker
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.sandbox.application.controller import (
    DATA_MOUNT,
    AuditEntry,
    SandboxController,
    SandboxRefused,
    SweepResult,
)
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import (
    DEFAULT_PROFILE,
    ResourceProfile,
    SandboxNetworking,
)
from tiny_hermes.sandbox.domain.models import NetworkPoolOverlap
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.infrastructure.lease_authority import SqlLeaseAuthority
from tiny_hermes.sandbox.infrastructure.sql_hosts import SqlSandboxHosts
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.sandbox.observation import observe_forever, publish_sandbox_gauges
from tiny_hermes.sandbox.transport.server import ControllerServer, ServerStream
from tiny_hermes.sandbox.transport.wiring import controller_listener
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.database import build_session_factory
from tiny_hermes.shared.logging import configure_logging
from tiny_hermes.shared.metrics import serve_configured_metrics
from tiny_hermes.shared.tracing import configure_tracing

logger = logging.getLogger(__name__)

#: docker-py's HTTP timeout, including a silent exec's hijacked stream.
#: Design §5.3: tools cap 900s plus a 30-second drain grace. The library
#: default of 60s is why a quiet large commit could die mid-write.
_DOCKER_CALL_TIMEOUT_SECONDS = 930


def _networking(settings: Settings) -> SandboxNetworking | None:
    """One network per sandbox, when the boundary is configured (§1.21.4, v2.21).

    All three are needed: a pool to take addresses from, the proxy's container
    to put on each network, and the URL the sandbox is told to use. Missing
    any, a sandbox has no network at all rather than a half-made one.
    """
    if settings.sandbox_egress_network and not settings.sandbox_network_pool:
        # The shared bridge this replaced let sandboxes reach each other.
        # Going quietly back to it would undo the fix; going offline says so.
        logger.warning(
            "SANDBOX_EGRESS_NETWORK is no longer read; sandboxes have no network until "
            "SANDBOX_NETWORK_POOL and SANDBOX_EGRESS_PROXY_CONTAINER are set (ops manual §10)"
        )
    if (
        not settings.sandbox_network_pool
        or not settings.sandbox_egress_proxy_container
        or not settings.egress_proxy_url
    ):
        return None
    pool = ip_network(settings.sandbox_network_pool)
    if not isinstance(pool, IPv4Network) or pool.prefixlen > 28:
        raise ValueError(
            f"SANDBOX_NETWORK_POOL={settings.sandbox_network_pool} must be IPv4 and hold at "
            "least one /28"
        )
    return SandboxNetworking(
        proxy_url=settings.egress_proxy_url,
        proxy_container=settings.sandbox_egress_proxy_container,
        pool=pool,
    )


def resource_ceiling(settings: Settings) -> ResourceProfile:
    """The operator's limits, threaded once at startup: with M1's single
    profile, "default" is whatever the instance configuration says it is."""
    return replace(
        DEFAULT_PROFILE,
        memory_mb=settings.sandbox_memory_mb,
        cache_mb=settings.sandbox_cache_mb,
        cache_inodes=settings.sandbox_cache_inodes,
    )


def memory_budget_mb(settings: Settings, *, meminfo: Path = Path("/proc/meminfo")) -> int:
    """The configured budget, or half this host's memory (§21.3).

    Read here and not in Settings: only the controller's host is the one whose
    memory the sandboxes use. A container reads the host's own MemTotal.
    """
    if settings.sandbox_memory_budget_mb is not None:
        return settings.sandbox_memory_budget_mb
    total_kib = next(
        int(line.split()[1])
        for line in meminfo.read_text(encoding="utf-8").splitlines()
        if line.startswith("MemTotal:")
    )
    half = total_kib // 1024 // 2
    if half < settings.sandbox_memory_mb:
        # Starting anyway would refuse every tool, forever, without saying why.
        raise ValueError(
            f"half of this host's memory is {half} MiB, less than one sandbox of "
            f"SANDBOX_MEMORY_MB={settings.sandbox_memory_mb}; lower that, or set "
            f"SANDBOX_MEMORY_BUDGET_MB"
        )
    return half


class _Registry(Protocol):
    async def register(
        self,
        *,
        name: str,
        url: str | None,
        memory_budget_mb: int | None,
        network_pool: str | None,
    ) -> Any: ...


def docker_runtimes(client: Any) -> tuple[str, ...]:
    """The OCI runtimes this Docker daemon can start containers with."""
    runtimes: dict[str, Any] = client.info().get("Runtimes") or {}
    return tuple(sorted(str(name) for name in runtimes))


def require_runtime(wanted: str, *, available: tuple[str, ...]) -> None:
    """Stop here when the configured runtime is not installed (§1.25, v2.25).

    Starting anyway would fail every sandbox, one acquire at a time, with an
    error naming the container rather than the setting.
    """
    if wanted not in available:
        raise SystemExit(
            f"SANDBOX_RUNTIME={wanted} is not a runtime this Docker has "
            f"({', '.join(available) or 'none listed'}); install it or name one of those"
        )


async def register_this_host(hosts: _Registry, settings: Settings, *, budget_mb: int) -> None:
    """Write this host's row before serving anything (tier C §4.1).

    An overlapping network pool stops the controller here, naming the other
    host: serving anyway would hand out addresses that host may hand out too,
    and the egress proxy, which names a Run by address alone, would charge one
    Run's traffic to another (§4.6).
    """
    try:
        await hosts.register(
            name=settings.sandbox_controller_name,
            url=settings.sandbox_controller_advertise_url,
            memory_budget_mb=budget_mb,
            network_pool=settings.sandbox_network_pool or None,
        )
    except NetworkPoolOverlap as overlap:
        raise SystemExit(
            f"SANDBOX_NETWORK_POOL={settings.sandbox_network_pool} overlaps sandbox host "
            f"{overlap.other}'s {overlap.other_pool}; give each host a range of its own"
        ) from overlap


async def heartbeat_forever(beat: Callable[[], Awaitable[None]], *, every: float) -> None:
    """Mark this host alive every `every` seconds, until cancelled.

    A failed beat is logged and the next one tries again: a host the database
    briefly could not hear is skipped by placement for a while, and nothing
    else follows from that (§4.8).
    """
    while True:
        await asyncio.sleep(every)
        try:
            await beat()
        except Exception:
            logger.warning("sandbox host heartbeat failed", exc_info=True)


def main() -> None:
    configure_logging()
    serve_configured_metrics(get_settings())
    configure_tracing(get_settings(), service="controller")
    asyncio.run(_serve())


async def _serve() -> None:
    settings = get_settings()
    sessions = build_session_factory(settings)
    ceiling = resource_ceiling(settings)
    budget = memory_budget_mb(settings)
    # Once, at start: a pool that cannot be used stops the process here, not
    # every acquire later, and a retired setting is warned about once.
    networking = _networking(settings)
    client: Any = docker.from_env(timeout=_DOCKER_CALL_TIMEOUT_SECONDS)  # noqa: TID251 - the one place, by design
    require_runtime(settings.sandbox_runtime, available=docker_runtimes(client))

    def controller_on(session: AsyncSession) -> SandboxController:
        return SandboxController(
            engine=DockerEngine(client),
            store=SqlSandboxStore(session),
            approved_digests=settings.approved_image_digests,
            leases=SqlLeaseAuthority(session),
            audit=_AuditSink(session),
            ceiling=ceiling,
            networking=networking,
            memory_budget_mb=budget,
            memory_ceiling_mb=settings.sandbox_ceiling_memory_mb,
            name=settings.sandbox_controller_name,
            runtime=settings.sandbox_runtime,
            # §21.3, v2.24: an acquire commits its registration, and with it
            # the admission lock, before Docker builds the container.
            commit=session.commit,
        )

    async def dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One session per call.

        A Controller holding one long-lived session would keep a transaction
        open across the seconds a container start takes, and a Worker's slice
        would then wait on a lock the Controller forgot it held.
        """
        async with sessions() as session:
            controller = controller_on(session)
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
            controller = controller_on(session)
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

    async with sessions() as session:
        await register_this_host(SqlSandboxHosts(session), settings, budget_mb=budget)
        await session.commit()

    async def beat() -> None:
        async with sessions() as session:
            await SqlSandboxHosts(session).heartbeat(settings.sandbox_controller_name)
            await session.commit()

    beating = asyncio.create_task(
        heartbeat_forever(beat, every=settings.sandbox_host_heartbeat_seconds)
    )

    address, tls = controller_listener(settings)
    server = ControllerServer(
        dispatch=dispatch,
        stream_dispatch=dispatch_stream,
        address=address,
        tls=tls,
        group=settings.sandbox_socket_gid,
    )
    await server.start()
    logger.info(
        "sandbox controller started",
        extra={
            "listen": str(address),
            "sandbox_memory_mb": ceiling.memory_mb,
            "sandbox_memory_budget_mb": budget,
            "sandbox_controller_name": settings.sandbox_controller_name,
        },
    )
    async def sweep_once() -> SweepResult:
        async with sessions() as session:
            result = await controller_on(session).sweep()
            await session.commit()
            return result

    sweeping = asyncio.create_task(sweep_forever(sweep_once, every=settings.sandbox_sweep_seconds))

    async def publish_once() -> None:
        async with sessions() as session:
            await publish_sandbox_gauges(SqlSandboxStore(session), budget_mb=budget)

    observing = asyncio.create_task(
        observe_forever(publish_once, every=settings.queue_observe_seconds)
    )
    stop = _stop_on_termination()
    try:
        await stop
    finally:
        observing.cancel()
        sweeping.cancel()
        beating.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeping
        await server.stop()
        client.close()
    logger.info("sandbox controller stopped")


async def sweep_forever(
    sweep_once: Callable[[], Awaitable[SweepResult]], *, every: float
) -> None:
    """Sweep now, then every `every` seconds, until cancelled.

    Now first: a controller that just started is most often one that restarted
    after the crash that left something behind. A failed pass is logged and
    the next one tries again; nothing here may take the controller down.
    """
    while True:
        try:
            result = await sweep_once()
        except Exception:
            logger.warning("sandbox host sweep failed", exc_info=True)
        else:
            if result.containers or result.volumes:
                logger.info(
                    "sandbox host swept",
                    extra={"containers": result.containers, "volumes": result.volumes},
                )
        await asyncio.sleep(every)


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
            # Absent from a Worker older than v2.22, and then the default.
            memory_mb=None if payload.get("memory_mb") is None else int(payload["memory_mb"]),
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
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(channel.keep_alive(stop))
    try:
        result = await controller.engine.execute_streamed(
            ticket.container_id, command, sink
        )
    finally:
        stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
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
