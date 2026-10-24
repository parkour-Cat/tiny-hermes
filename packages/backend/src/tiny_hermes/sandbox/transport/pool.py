"""A pool of sandbox hosts, as the Worker and the Scheduler hold it (tier C §4.3, §4.4).

The same surface as `SandboxClient`, so neither runtime knows whether there is
one host or several. Every call but `acquire` names a sandbox, and a sandbox
never moves, so it goes to the host the sandbox was made on. `acquire` goes to
the host of the Run's live claim if it has one, and otherwise down the
placement order.

Placement only picks a host likely to admit; the host's own admission is the
authority. So the pool moves to the next host only when it is certain nothing
happened on the one it asked: refused for room, or never connected. A call
that may have been received is left to the recovery flow, because the host
may have made the sandbox and failed to say so.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from tiny_hermes.sandbox.application.controller import (
    RefusalReason,
    SandboxRefused,
    SandboxUnreachable,
)
from tiny_hermes.sandbox.domain.command import (
    CommandResult,
    SandboxCommand,
    ScannedEntry,
    StreamedResult,
)
from tiny_hermes.sandbox.domain.models import SandboxInstance
from tiny_hermes.sandbox.transport.adapter import AcquiredSandbox, SandboxClient
from tiny_hermes.sandbox.transport.client import ControllerClient

logger = logging.getLogger(__name__)


class HostDirectory(Protocol):
    async def host_of_run(self, run_id: UUID) -> str | None: ...

    async def host_of_sandbox(self, sandbox_id: UUID) -> str | None: ...

    async def placement(self) -> list[str]: ...

    async def url_of(self, name: str) -> str | None: ...


class SandboxPool:
    def __init__(
        self, directory: HostDirectory, *, connect: Callable[[str], SandboxClient]
    ) -> None:
        self._directory = directory
        self._connect = connect
        self._clients: dict[str, SandboxClient] = {}
        #: Where each sandbox lives. Never invalidated: an instance is made on
        #: one host and stays there until it is destroyed.
        self._homes: dict[UUID, str] = {}

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
        memory_mb: int | None = None,
    ) -> AcquiredSandbox:
        claimed = await self._directory.host_of_run(run_id)
        # A Run with a live claim goes back to it and nowhere else: a frozen
        # sandbox thaws only where it is, and an isolated one must not be
        # replaced by a second sandbox on another host.
        hosts = [claimed] if claimed is not None else await self._directory.placement()
        exhausted: SandboxRefused | None = None
        unreachable: SandboxUnreachable | None = None
        for host in hosts:
            try:
                try:
                    client = await self._client(host)
                except ControllerClient.NotConnected as nowhere:
                    raise SandboxUnreachable(str(nowhere), sent=False) from nowhere
                made = await client.acquire(
                    run_id=run_id,
                    lease_id=lease_id,
                    workspace_id=workspace_id,
                    profile=profile,
                    session_id=session_id,
                    memory_mb=memory_mb,
                )
            except SandboxRefused as refused:
                if refused.reason is not RefusalReason.MEMORY_BUDGET_EXHAUSTED:
                    raise
                exhausted = refused
            except SandboxUnreachable as silent:
                if silent.sent:
                    raise
                unreachable = silent
            else:
                self._homes[made.sandbox_id] = host
                return made
        if exhausted is not None or unreachable is None:
            # Full somewhere, or nowhere to place at all (every host draining
            # or silent): either way the Run waits its turn rather than fails.
            if not hosts:
                logger.warning("no sandbox host is taking new sandboxes")
            raise exhausted or SandboxRefused(RefusalReason.MEMORY_BUDGET_EXHAUSTED)
        raise unreachable

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        client = await self._home(sandbox_id)
        return await client.execute(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id, command=command
        )

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        client = await self._home(sandbox_id)
        await client.freeze(run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id)

    async def thaw(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        client = await self._home(sandbox_id)
        await client.thaw(run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id)

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        client = await self._home(sandbox_id)
        await client.destroy(run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id)

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None:
        client = await self._home(sandbox_id)
        await client.keep(run_id=run_id, sandbox_id=sandbox_id, until=until)

    async def inspect(self, *, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        client = await self._home(sandbox_id)
        return await client.inspect(run_id=run_id, sandbox_id=sandbox_id)

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        client = await self._home(sandbox_id)
        await client.cleanup(run_id=run_id, sandbox_id=sandbox_id)

    async def volume_remove(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        client = await self._home(sandbox_id)
        await client.volume_remove(run_id=run_id, sandbox_id=sandbox_id)

    async def workspace_import(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        tar_stream: AsyncIterator[bytes],
        declared_total: int,
    ) -> dict[str, Any]:
        client = await self._home(sandbox_id)
        return await client.workspace_import(
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            tar_stream=tar_stream,
            declared_total=declared_total,
        )

    async def workspace_scan(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> tuple[ScannedEntry, ...]:
        client = await self._home(sandbox_id)
        return await client.workspace_scan(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id
        )

    async def workspace_export(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        sink: Callable[[bytes], Awaitable[None]],
        limit: int,
    ) -> dict[str, Any]:
        client = await self._home(sandbox_id)
        return await client.workspace_export(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id, sink=sink, limit=limit
        )

    async def execute_stream(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        command: SandboxCommand,
        artifact_limit: int,
        sink: Callable[[bytes], Awaitable[None]],
    ) -> StreamedResult:
        client = await self._home(sandbox_id)
        return await client.execute_stream(
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            command=command,
            artifact_limit=artifact_limit,
            sink=sink,
        )

    async def _home(self, sandbox_id: UUID) -> SandboxClient:
        host = self._homes.get(sandbox_id)
        if host is None:
            host = await self._directory.host_of_sandbox(sandbox_id)
            if host is None:
                # What its host would have answered, without guessing a host.
                raise SandboxRefused(RefusalReason.NO_RESERVATION)
            self._homes[sandbox_id] = host
        return await self._client(host)

    async def _client(self, host: str) -> SandboxClient:
        found = self._clients.get(host)
        if found is not None:
            return found
        url = await self._directory.url_of(host)
        if url is None:
            # A host registered on a socket: nobody else can dial it. Raised as
            # the transport would raise a connection it could not make, which
            # is what every caller already handles.
            raise ControllerClient.NotConnected(f"sandbox host {host} has no address")
        client = self._connect(url)
        self._clients[host] = client
        return client

