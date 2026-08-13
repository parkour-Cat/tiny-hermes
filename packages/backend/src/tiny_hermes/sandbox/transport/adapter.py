"""The Controller, as the Worker and the Scheduler hold it.

Turns the six actions into socket calls and back. It exists so those two
runtimes can be written against one shape whether the Controller is in-process
(as it is in tests, against a real Docker daemon) or across a socket (as it is
in a deployment).

It contains no rules, for the same reason the client does not: a rule here
would be a rule the Controller could not enforce, and the Controller is the
only thing on the far side that cannot be bypassed.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from tiny_hermes.sandbox.application.controller import RefusalReason, SandboxRefused
from tiny_hermes.sandbox.domain.command import (
    CommandResult,
    SandboxCommand,
    ScannedEntry,
    StreamedResult,
)
from tiny_hermes.sandbox.domain.models import CacheState, InstanceStatus, SandboxInstance
from tiny_hermes.sandbox.transport.client import ControllerClient


@dataclass(frozen=True)
class AcquiredSandbox:
    sandbox_id: UUID
    cache_state: CacheState


class SandboxClient:
    def __init__(self, client: ControllerClient) -> None:
        self._client = client

    async def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One call, with the refusal put back into the platform's own type.

        The socket carries a refusal as a string, and a caller that only caught
        `SandboxRefused` would meet `ControllerClient.Refused` instead and treat
        a decided "no" as an unknown failure. That is not hypothetical: the
        restart drill found the Worker failing a recoverable Run because
        `already_reserved` arrived as text across the socket and as an enum in
        the tests.
        """
        try:
            return await self._client.call(action, payload)
        except ControllerClient.Refused as refused:
            try:
                reason = RefusalReason(refused.reason)
            except ValueError:
                raise
            raise SandboxRefused(reason) from refused

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
    ) -> AcquiredSandbox:
        answer = await self._call(
            "acquire",
            {
                "run_id": run_id,
                "lease_id": lease_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "profile": profile,
            },
        )
        return AcquiredSandbox(
            sandbox_id=UUID(str(answer["sandbox_id"])),
            cache_state=CacheState(str(answer["cache_state"])),
        )

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        payload = {
            "run_id": run_id,
            "lease_id": lease_id,
            "sandbox_id": sandbox_id,
            "command": {
                "argv": command.argv,
                "cwd": command.cwd,
                "timeout_seconds": command.timeout_seconds,
                "output_limit": command.output_limit,
            },
        }
        if command.stdin is None:
            answer = await self._call("execute", payload)
        else:
            # A write body never rides the control line; it goes as frames.
            try:
                answer = await self._client.send_stream(
                    "execute_stdin",
                    payload,
                    _one_chunk(command.stdin),
                    declared_total=len(command.stdin),
                )
            except ControllerClient.Refused as refused:
                raise self._refusal(refused) from refused
        return CommandResult(
            exit_code=int(answer["exit_code"]),
            output=str(answer["output"]),
            truncated=bool(answer["truncated"]),
            timed_out=bool(answer["timed_out"]),
        )

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._call(
            "freeze",
            {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id},
        )

    async def thaw(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._call(
            "thaw", {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id}
        )

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._call(
            "destroy",
            {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id},
        )

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None:
        await self._call(
            "keep",
            {"run_id": run_id, "sandbox_id": sandbox_id, "until": until.isoformat()},
        )

    async def inspect(self, *, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        answer = await self._call(
            "inspect", {"run_id": run_id, "sandbox_id": sandbox_id}
        )
        return SandboxInstance(
            id=sandbox_id,
            container_id="",
            image_digest="",
            resource_profile="",
            boot_id=str(answer["boot_id"]),
            status=InstanceStatus(str(answer["status"])),
        )

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        await self._call("cleanup", {"run_id": run_id, "sandbox_id": sandbox_id})

    # -- Workspace actions -----------------------------------------------------

    async def workspace_import(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        tar_stream: AsyncIterator[bytes],
        declared_total: int,
    ) -> dict[str, Any]:
        try:
            return await self._client.send_stream(
                "workspace_import",
                {
                    "run_id": run_id,
                    "lease_id": lease_id,
                    "sandbox_id": sandbox_id,
                    "declared_total": declared_total,
                },
                tar_stream,
                declared_total=declared_total,
            )
        except ControllerClient.Refused as refused:
            raise self._refusal(refused) from refused

    async def workspace_scan(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> tuple[ScannedEntry, ...]:
        answer = await self._call(
            "workspace_scan",
            {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id},
        )
        entries: Any = answer.get("entries", [])
        return tuple(
            ScannedEntry(
                path=str(entry["path"]),
                entry_type=str(entry["type"]),
                mode=int(entry["mode"]),
                size=int(entry["size"]),
                sha256=None if entry.get("sha256") is None else str(entry["sha256"]),
            )
            for entry in entries
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
        try:
            return await self._client.receive_stream(
                "workspace_export",
                {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id},
                sink,
                limit=limit,
            )
        except ControllerClient.Refused as refused:
            raise self._refusal(refused) from refused

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
        try:
            answer = await self._client.receive_stream(
                "execute_stream",
                {
                    "run_id": run_id,
                    "lease_id": lease_id,
                    "sandbox_id": sandbox_id,
                    "artifact_limit": artifact_limit,
                    "command": {
                        "argv": command.argv,
                        "cwd": command.cwd,
                        "timeout_seconds": command.timeout_seconds,
                        "output_limit": command.output_limit,
                    },
                },
                sink,
                limit=artifact_limit,
            )
        except ControllerClient.Refused as refused:
            raise self._refusal(refused) from refused
        return StreamedResult(
            exit_code=int(answer["exit_code"]),
            timed_out=bool(answer["timed_out"]),
            observed_bytes=int(answer["observed_bytes"]),
            delivered_bytes=int(answer["delivered_bytes"]),
            truncated=bool(answer["truncated"]),
        )

    async def volume_remove(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        await self._call(
            "volume_remove", {"run_id": run_id, "sandbox_id": sandbox_id}
        )

    def _refusal(self, refused: ControllerClient.Refused) -> Exception:
        try:
            return SandboxRefused(RefusalReason(refused.reason))
        except ValueError:
            return refused


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data
