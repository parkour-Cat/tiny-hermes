"""The only module in this process that speaks to a Docker daemon.

docker-py is synchronous, so every call goes through `asyncio.to_thread`. That
is a deliberate choice over an async client: docker-py speaks the Windows named
pipe and the async alternatives do not, and this repository is developed on
Windows. A thread per Docker call is not a cost worth optimizing when the calls
themselves take tens of milliseconds and a container start takes hundreds.

This module holds no rules. It creates what it is told to create; deciding what
may be created is `domain/container_policy.py`, and deciding who may ask is
`application/controller.py`.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import ContainerConfig


class DockerUnavailable(Exception):
    """The daemon could not be reached, or refused.

    Distinct from a refusal: a refusal is the platform saying no, and this is
    the platform being unable to ask.
    """


@dataclass
class DockerEngine:
    client: Any
    #: Added to every container this engine creates. The test suite uses it to
    #: sweep; a deployment can use it to find what a crashed Controller left.
    extra_labels: dict[str, str] | None = None

    async def create(self, config: ContainerConfig) -> str:
        kwargs = config.as_docker_kwargs()
        if self.extra_labels:
            kwargs["labels"] = {**kwargs["labels"], **self.extra_labels}
        container = await self._call(self.client.containers.run, **kwargs)
        return str(container.id)

    async def execute(self, container_id: str, command: SandboxCommand) -> CommandResult:
        container = await self._call(self.client.containers.get, container_id)
        handle = await self._call(
            self.client.api.exec_create,
            container.id,
            cmd=command.argv,
            workdir=command.cwd,
            stdout=True,
            stderr=True,
            # No tty: a tty merges streams through a pty and rewrites newlines,
            # which turns exact output into approximately-right output.
            tty=False,
        )
        try:
            raw: bytes = await asyncio.wait_for(
                self._call(self.client.api.exec_start, handle["Id"], demux=False),
                timeout=command.timeout_seconds,
            )
        except TimeoutError:
            # The exec is still running inside the container. It will be
            # collected when the container is destroyed, and the Run is told
            # what happened rather than left waiting.
            return CommandResult(exit_code=124, output="", truncated=False, timed_out=True)

        truncated = len(raw) > command.output_limit
        body = raw[: command.output_limit]
        if truncated:
            body += b"\n[output truncated by the platform]"
        info = await self._call(self.client.api.exec_inspect, handle["Id"])
        return CommandResult(
            exit_code=int(info.get("ExitCode") or 0),
            output=body.decode("utf-8", errors="replace"),
            truncated=truncated,
            timed_out=False,
        )

    async def pause(self, container_id: str) -> None:
        container = await self._call(self.client.containers.get, container_id)
        await self._call(container.pause)

    async def unpause(self, container_id: str) -> None:
        container = await self._call(self.client.containers.get, container_id)
        await self._call(container.unpause)

    async def remove(self, container_id: str) -> None:
        container = await self._call(self.client.containers.get, container_id)
        await self._call(container.remove, force=True)

    async def _call(self, work: Any, *args: Any, **kwargs: Any) -> Any:
        from docker.errors import DockerException  # noqa: PLC0415 - narrow the import

        try:
            return await asyncio.to_thread(lambda: work(*args, **kwargs))
        except DockerException as failure:
            raise DockerUnavailable(str(failure)) from failure
