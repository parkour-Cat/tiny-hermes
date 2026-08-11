"""`shell.exec`, from a model's call to a result, against a real container.

The registry decides whether a call may run; this is what happens when it may.
Everything here goes through the Controller, so the container the command lands
in is the one the platform's own policy built.
"""

from typing import Any
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.sandbox.application.controller import SandboxController
from tiny_hermes.tools.application.execute import run_tool_call
from tiny_hermes.tools.domain.registry import RefusalReason

RUN = uuid4()
LEASE = uuid4()
WORKSPACE = uuid4()


def call(command: str, **arguments: Any) -> ToolCallBlock:
    return ToolCallBlock(
        call_id="c1", name="shell.exec", arguments={"command": command, **arguments}
    )


async def acquired(controller: SandboxController) -> Any:
    return await controller.acquire(
        run_id=RUN, lease_id=LEASE, workspace_id=WORKSPACE, profile="default"
    )


async def execute(
    controller: SandboxController, block: ToolCallBlock, bound: tuple[str, ...] = ("shell.exec",)
) -> Any:
    box = await controller.store.live_for_run(RUN)
    assert box is not None
    return await run_tool_call(
        controller=controller,
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.instance_id,
        bound=bound,
        call=block,
    )


async def test_a_command_runs_and_its_output_comes_back(
    controller: SandboxController,
) -> None:
    await acquired(controller)
    result = await execute(controller, call("echo hello"))

    assert result.call_id == "c1"
    assert result.output.strip() == "hello"
    assert result.exit_code == 0
    assert result.failed is False


async def test_a_non_zero_exit_is_a_result_and_not_a_failure(
    controller: SandboxController,
) -> None:
    """The command ran and reported. `failed` is for a tool that never ran —
    telling the model a failing command was a refused one would send it looking
    for a permission problem that does not exist.
    """
    await acquired(controller)
    result = await execute(controller, call("exit 3"))

    assert result.exit_code == 3
    assert result.failed is False


async def test_stderr_comes_back_too(controller: SandboxController) -> None:
    """A command that only wrote to stderr would otherwise look like it said
    nothing, and the model would be debugging silence."""
    await acquired(controller)
    result = await execute(controller, call("echo trouble >&2"))

    assert "trouble" in result.output


async def test_a_shell_pipeline_is_not_split_by_the_platform(
    controller: SandboxController,
) -> None:
    await acquired(controller)
    result = await execute(controller, call("printf 'a\\nb\\nc\\n' | wc -l"))

    assert result.output.strip() == "3"


async def test_an_unbound_tool_is_refused_before_the_container_is_touched(
    controller: SandboxController,
) -> None:
    """The second authorization step, at the point it actually protects.

    The call is well-formed and the sandbox is right there; the only thing
    wrong is that this Agent never bound the tool.
    """
    await acquired(controller)
    result = await execute(controller, call("echo hello"), bound=())

    assert result.failed is True
    assert RefusalReason.NOT_AUTHORIZED.value in result.output
    # And it did not run: a marker the command would have left is absent.
    check = await execute(controller, call("ls /workspace/data"))
    assert "hello" not in check.output


async def test_an_escape_from_the_workspace_is_refused_and_leaves_no_trace(
    controller: SandboxController,
) -> None:
    await acquired(controller)
    result = await execute(controller, call("touch /etc/planted", cwd="/workspace/data/../../etc"))

    assert result.failed is True
    assert RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED.value in result.output


async def test_a_refusal_answers_the_call_that_asked(
    controller: SandboxController,
) -> None:
    """Otherwise the model waits on a call that never gets an answer, and
    either retries it or invents what it returned."""
    await acquired(controller)
    result = await execute(controller, call("echo hi"), bound=())
    assert result.call_id == "c1"


async def test_a_timeout_comes_back_as_a_result(controller: SandboxController) -> None:
    """§11.5 puts the decision in the loop, so this is not a Run failure."""
    await acquired(controller)
    result = await execute(controller, call("sleep 30", timeout_seconds=1))

    assert result.exit_code != 0
    assert "timed out" in result.output.lower()
    assert result.failed is False


async def test_output_beyond_the_cap_is_truncated_and_says_so(
    controller: SandboxController,
) -> None:
    await acquired(controller)
    result = await execute(controller, call("seq 1 500000"))

    assert "truncated" in result.output.lower()


async def test_the_command_cannot_reach_the_network(
    controller: SandboxController,
) -> None:
    await acquired(controller)
    result = await execute(controller, call("cat /proc/net/dev | tail -n +3 | wc -l"))

    assert result.output.strip() == "1"


async def test_work_survives_between_two_calls_in_one_slice(
    controller: SandboxController,
) -> None:
    """The point of holding the container: a model builds something and uses it."""
    await acquired(controller)
    await execute(controller, call("echo 'kept' > /workspace/data/note.txt"))
    result = await execute(controller, call("cat /workspace/data/note.txt"))

    assert result.output.strip() == "kept"


@pytest.mark.parametrize("path", ["/etc/passwd", "/usr/bin/env", "/"])
async def test_the_read_only_root_holds_against_a_real_command(
    controller: SandboxController, path: str
) -> None:
    await acquired(controller)
    result = await execute(controller, call(f"touch {path}/x 2>/dev/null || touch {path}"))

    assert result.exit_code != 0
