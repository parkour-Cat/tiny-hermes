"""One tool call, from what the model asked to what it is told back.

Every path out of here is a `ToolResultBlock`. A refusal, a timeout, a command
that exited non-zero and a Controller that could not be reached all become
something the model can read and act on, because the alternative — raising into
the Worker's loop — turns one bad call into a failed Run.

The one exception is deliberate: an unreachable Controller is *not* the model's
problem to solve, so it raises. §11.5 makes that `interrupted`, and a Run that
is interrupted can be recovered, while a Run told "the sandbox is broken" would
have the model politely try again forever.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.runs.domain.models import ToolCallBlock, ToolResultBlock
from tiny_hermes.sandbox.application.controller import SandboxRefused
from tiny_hermes.sandbox.domain.command import (
    CommandResult,
    SandboxCommand,
    StreamedResult,
)
from tiny_hermes.tools.domain.files import FILE_ARGUMENTS, TRUNCATED_EXIT
from tiny_hermes.tools.domain.registry import DEFAULT_OUTPUT_BYTES, ToolRefused, authorize


class CommandRunner(Protocol):
    """The one thing this needs from the Controller.

    Narrow on purpose: an executor that could freeze or destroy a sandbox would
    be a second place deciding a container's lifetime, and the Worker is the
    one that knows when a slice ends.
    """

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult: ...


class StreamedCommandRunner(Protocol):
    """The Controller's framed execute, as the Worker holds it over the socket.

    Distinct from `SandboxController.execute_stream`, which only authorizes and
    returns a ticket: this one takes a sink and drains, matching `SandboxClient`.
    """

    async def execute_stream(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        command: SandboxCommand,
        artifact_limit: int,
        sink: Callable[[bytes], Awaitable[None]],
    ) -> StreamedResult: ...


class ArtifactUpload(Protocol):
    """The recorder's write half, as this module needs it.

    Registration happens on the first `deliver`; `finish` either returns the
    committed Artifact or leaves the debt for the collector.
    """

    async def deliver(self, chunk: bytes) -> None: ...

    async def finish(self, *, truncated: bool) -> Artifact | None: ...


async def run_tool_call(
    *,
    controller: CommandRunner,
    run_id: UUID,
    lease_id: UUID,
    sandbox_id: UUID,
    bound: tuple[str, ...],
    call: ToolCallBlock,
    streamer: StreamedCommandRunner | None = None,
    open_artifact: Callable[[], ArtifactUpload] | None = None,
    preview_limit: int = DEFAULT_OUTPUT_BYTES,
    artifact_limit: int | None = None,
) -> ToolResultBlock:
    try:
        authorized = authorize(bound=bound, call=call)
    except ToolRefused as refused:
        return _refusal(call.call_id, refused.reason.value, refused.detail)

    if streamer is not None and authorized.name not in FILE_ARGUMENTS:
        return await _run_streamed(
            streamer=streamer,
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            call_id=call.call_id,
            command=authorized.command,
            open_artifact=open_artifact,
            preview_limit=preview_limit,
            artifact_limit=artifact_limit,
        )

    try:
        result = await controller.execute(
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            command=authorized.command,
        )
    except SandboxRefused as refused:
        # The Controller's own checks, reached with an authorized call. This is
        # the platform disagreeing with itself rather than the model asking for
        # something wrong, but the model still needs an answer.
        return _refusal(call.call_id, refused.reason.value, "")

    return _from_command(call.call_id, authorized.name, authorized.command, result)


async def _run_streamed(
    *,
    streamer: StreamedCommandRunner,
    run_id: UUID,
    lease_id: UUID,
    sandbox_id: UUID,
    call_id: str,
    command: SandboxCommand,
    open_artifact: Callable[[], ArtifactUpload] | None,
    preview_limit: int,
    artifact_limit: int | None,
) -> ToolResultBlock:
    cap = artifact_limit if artifact_limit is not None else preview_limit
    if open_artifact is not None and artifact_limit is None:
        cap = max(preview_limit, 104_857_600)
    sink = PreviewingSink(
        preview_limit=preview_limit,
        artifact_limit=cap,
        open_artifact=open_artifact,
    )
    try:
        streamed = await streamer.execute_stream(
            run_id=run_id,
            lease_id=lease_id,
            sandbox_id=sandbox_id,
            command=command,
            artifact_limit=sink.artifact_limit,
            sink=sink.deliver,
        )
    except SandboxRefused as refused:
        return _refusal(call_id, refused.reason.value, "")

    artifact = await sink.finish(truncated=streamed.truncated)
    return _from_stream(
        call_id,
        bytes(sink.preview),
        streamed,
        command.timeout_seconds,
        artifact,
        store_failed=sink.store_failed,
    )


def _from_command(
    call_id: str, name: str, command: SandboxCommand, result: CommandResult
) -> ToolResultBlock:
    if result.timed_out:
        # Not `failed`: the command ran, and how long to allow is a decision the
        # loop makes with the rest of the budget in view.
        return ToolResultBlock(
            call_id=call_id,
            output=(
                f"{result.output}\n[the command timed out after "
                f"{command.timeout_seconds}s and was stopped]"
            ).strip(),
            exit_code=result.exit_code,
            failed=False,
        )

    if name in FILE_ARGUMENTS:
        return _file_result(call_id, result)

    return ToolResultBlock(
        call_id=call_id,
        output=result.output,
        exit_code=result.exit_code,
        failed=False,
    )


def _from_stream(
    call_id: str,
    preview: bytes,
    streamed: StreamedResult,
    timeout_seconds: int,
    artifact: Artifact | None,
    *,
    store_failed: bool,
) -> ToolResultBlock:
    output = preview.decode("utf-8", errors="replace")
    notes: list[str] = []
    if streamed.timed_out:
        notes.append(
            f"[the command timed out after {timeout_seconds}s and was stopped]"
        )
    if store_failed:
        notes.append("artifact_store_failed")
    elif artifact is not None:
        notes.append(f"artifact_id={artifact.id}")
        truncated = artifact.truncated or streamed.truncated
        notes.append(f"artifact_truncated={'true' if truncated else 'false'}")
    elif streamed.truncated:
        notes.append("[output truncated by the platform]")
    if notes:
        output = f"{output.rstrip()}\n" + "\n".join(notes) if output else "\n".join(notes)
    return ToolResultBlock(
        call_id=call_id,
        output=output,
        exit_code=streamed.exit_code,
        failed=False,
    )


class PreviewingSink:
    """Keeps the inline preview, and opens an Artifact only once it overflows.

    Design §11: the first 1 MiB stays in the tool result; bytes past it are
    registered before they are uploaded. The engine's ceiling is the Artifact
    cap, not the preview, so a noisy child is drained rather than blocked.
    """

    def __init__(
        self,
        *,
        preview_limit: int,
        artifact_limit: int,
        open_artifact: Callable[[], ArtifactUpload] | None,
    ) -> None:
        self.preview = bytearray()
        self.store_failed = False
        self.artifact_limit = artifact_limit
        self._preview_limit = preview_limit
        self._open = open_artifact
        self._recorder: ArtifactUpload | None = None

    async def deliver(self, chunk: bytes) -> None:
        if self.store_failed:
            return
        if self._recorder is not None:
            await self._push(chunk)
            return
        room = self._preview_limit - len(self.preview)
        if len(chunk) <= room:
            self.preview.extend(chunk)
            return
        self.preview.extend(chunk[:room])
        leftover = bytes(chunk[room:])
        if self._open is None:
            return
        self._recorder = self._open()
        await self._push(bytes(self.preview))
        if leftover:
            await self._push(leftover)

    async def _push(self, chunk: bytes) -> None:
        recorder = self._recorder
        if recorder is None or self.store_failed:
            return
        try:
            await recorder.deliver(chunk)
        except Exception:  # noqa: BLE001 - any store failure keeps the preview
            self.store_failed = True

    async def finish(self, *, truncated: bool) -> Artifact | None:
        recorder = self._recorder
        if recorder is None:
            return None
        try:
            return await recorder.finish(truncated=truncated)
        except Exception:  # noqa: BLE001 - the preview is the answer we still have
            self.store_failed = True
            return None


def _file_result(call_id: str, result: CommandResult) -> ToolResultBlock:
    """The helper's exit codes, translated into answers a model can act on."""
    if result.exit_code == 0:
        return ToolResultBlock(
            call_id=call_id, output=result.output, exit_code=0, failed=False
        )
    if result.exit_code == TRUNCATED_EXIT:
        # The bytes are a prefix, and the model is told so — a shorter answer
        # it could not tell from a complete one would be a quiet lie.
        return ToolResultBlock(
            call_id=call_id,
            output=f"{result.output}\n[truncated at the per-call byte limit]",
            exit_code=0,
            failed=False,
        )
    if result.exit_code == 1:
        # The helper refused the input outright; same vocabulary as the
        # registry's own refusals.
        return _refusal(call_id, "tool_not_authorized", "")
    return ToolResultBlock(
        call_id=call_id,
        output=result.output or "the file operation failed",
        exit_code=result.exit_code,
        failed=True,
    )


def _refusal(call_id: str, reason: str, detail: str) -> ToolResultBlock:
    """A refusal the model can read.

    The reason is named rather than described, so an Agent that meets the same
    wall twice can tell it is the same wall — and so a person reading the
    transcript can grep for it.
    """
    body = f"refused: {reason}"
    return ToolResultBlock(
        call_id=call_id,
        output=f"{body} ({detail})" if detail else body,
        exit_code=126,
        failed=True,
    )
