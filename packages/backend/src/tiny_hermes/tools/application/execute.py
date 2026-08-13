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

from typing import Protocol
from uuid import UUID

from tiny_hermes.runs.domain.models import ToolCallBlock, ToolResultBlock
from tiny_hermes.sandbox.application.controller import SandboxRefused
from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand
from tiny_hermes.tools.domain.files import FILE_ARGUMENTS, TRUNCATED_EXIT
from tiny_hermes.tools.domain.registry import ToolRefused, authorize


class CommandRunner(Protocol):
    """The one thing this needs from the Controller.

    Narrow on purpose: an executor that could freeze or destroy a sandbox would
    be a second place deciding a container's lifetime, and the Worker is the
    one that knows when a slice ends.
    """

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult: ...


async def run_tool_call(
    *,
    controller: CommandRunner,
    run_id: UUID,
    lease_id: UUID,
    sandbox_id: UUID,
    bound: tuple[str, ...],
    call: ToolCallBlock,
) -> ToolResultBlock:
    try:
        authorized = authorize(bound=bound, call=call)
    except ToolRefused as refused:
        return _refusal(call.call_id, refused.reason.value, refused.detail)

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

    if result.timed_out:
        # Not `failed`: the command ran, and how long to allow is a decision the
        # loop makes with the rest of the budget in view.
        return ToolResultBlock(
            call_id=call.call_id,
            output=(
                f"{result.output}\n[the command timed out after "
                f"{authorized.command.timeout_seconds}s and was stopped]"
            ).strip(),
            exit_code=result.exit_code,
            failed=False,
        )

    if authorized.name in FILE_ARGUMENTS:
        return _file_result(call.call_id, result)

    return ToolResultBlock(
        call_id=call.call_id,
        output=result.output,
        exit_code=result.exit_code,
        failed=False,
    )


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
