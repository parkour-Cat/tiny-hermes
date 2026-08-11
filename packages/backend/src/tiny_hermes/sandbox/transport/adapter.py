"""The Controller, as the Worker and the Scheduler hold it.

Turns the six actions into socket calls and back. It exists so those two
runtimes can be written against one shape whether the Controller is in-process
(as it is in tests, against a real Docker daemon) or across a socket (as it is
in a deployment).

It contains no rules, for the same reason the client does not: a rule here
would be a rule the Controller could not enforce, and the Controller is the
only thing on the far side that cannot be bypassed.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand
from tiny_hermes.sandbox.domain.models import CacheState, InstanceStatus, SandboxInstance
from tiny_hermes.sandbox.transport.client import ControllerClient


@dataclass(frozen=True)
class AcquiredSandbox:
    sandbox_id: UUID
    cache_state: CacheState


class SandboxClient:
    def __init__(self, client: ControllerClient) -> None:
        self._client = client

    async def acquire(
        self, *, run_id: UUID, lease_id: UUID, workspace_id: UUID, profile: str
    ) -> AcquiredSandbox:
        answer = await self._client.call(
            "acquire",
            {
                "run_id": run_id,
                "lease_id": lease_id,
                "workspace_id": workspace_id,
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
        answer = await self._client.call(
            "execute",
            {
                "run_id": run_id,
                "lease_id": lease_id,
                "sandbox_id": sandbox_id,
                "command": {
                    "argv": command.argv,
                    "cwd": command.cwd,
                    "timeout_seconds": command.timeout_seconds,
                    "output_limit": command.output_limit,
                },
            },
        )
        return CommandResult(
            exit_code=int(answer["exit_code"]),
            output=str(answer["output"]),
            truncated=bool(answer["truncated"]),
            timed_out=bool(answer["timed_out"]),
        )

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._client.call(
            "freeze",
            {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id},
        )

    async def thaw(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._client.call(
            "thaw", {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id}
        )

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._client.call(
            "destroy",
            {"run_id": run_id, "lease_id": lease_id, "sandbox_id": sandbox_id},
        )

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None:
        await self._client.call(
            "keep",
            {"run_id": run_id, "sandbox_id": sandbox_id, "until": until.isoformat()},
        )

    async def inspect(self, *, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        answer = await self._client.call(
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
        await self._client.call("cleanup", {"run_id": run_id, "sandbox_id": sandbox_id})
