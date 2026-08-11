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

from uuid import UUID

from tiny_hermes.runs.domain.models import ToolCallBlock, ToolResultBlock
from tiny_hermes.sandbox.application.controller import (
    SandboxController,
    SandboxRefused,
)
from tiny_hermes.tools.domain.registry import ToolRefused, authorize


async def run_tool_call(
    *,
    controller: SandboxController,
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

    return ToolResultBlock(
        call_id=call.call_id,
        output=result.output,
        exit_code=result.exit_code,
        failed=False,
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
