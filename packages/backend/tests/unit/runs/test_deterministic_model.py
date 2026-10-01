import pytest
from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    TextBlock,
    ToolResultBlock,
)
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.ports.model import ModelRequest, StopReason

from ..agents.test_agent_models import valid_spec


def _request(scenario: str, round_index: int) -> ModelRequest:
    values = {
        **valid_spec(),
        "model_policy": {"provider": "deterministic", "scenario": scenario},
    }
    spec = AgentSpec.model_validate(values)
    return ModelRequest(
        policy=spec.model_policy,
        personality=spec.personality,
        messages=(CanonicalMessage(role="user", blocks=(TextBlock(text="do the thing"),)),),
        round_index=round_index,
    )


async def test_complete_finishes_in_one_round() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(_request("complete", 1))

    assert response.stop_reason is StopReason.COMPLETED
    assert response.text
    assert response.replay_safe is True
    assert response.model_calls == 1


async def test_fail_replay_safe_fails_without_an_unknown_effect() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(
        _request("fail_replay_safe", 1)
    )

    assert response.stop_reason is StopReason.FAILED
    assert response.replay_safe is True
    assert response.external_effect_unknown is False


async def test_continue_once_needs_a_second_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    first = await provider.complete(_request("continue_once", 1))
    second = await provider.complete(_request("continue_once", 2))

    assert first.stop_reason is StopReason.CONTINUE
    assert second.stop_reason is StopReason.COMPLETED


async def test_continue_once_never_continues_forever() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    for round_index in range(2, 6):
        response = await provider.complete(_request("continue_once", round_index))
        assert response.stop_reason is StopReason.COMPLETED


async def test_shell_once_asks_for_a_long_command_then_reads_its_real_output() -> None:
    values = {
        **valid_spec(),
        "model_policy": {"provider": "deterministic", "scenario": "shell_once"},
        "tools": ["shell.exec"],
    }
    spec = AgentSpec.model_validate(values)
    provider = DeterministicModelProvider(delay_ms=0)
    user = CanonicalMessage(role="user", blocks=(TextBlock(text="run the drill"),))

    first = await provider.complete(
        ModelRequest(
            policy=spec.model_policy,
            personality=spec.personality,
            messages=(user,),
            round_index=1,
            tools=({"type": "function", "function": {"name": "shell.exec"}},),
        )
    )

    assert first.stop_reason is StopReason.TOOL_CALL
    assert first.tool_calls[0].name == "shell.exec"
    assert "sleep 20" in str(first.tool_calls[0].arguments["command"])

    call = first.tool_calls[0]
    second = await provider.complete(
        ModelRequest(
            policy=spec.model_policy,
            personality=spec.personality,
            messages=(
                user,
                CanonicalMessage(role="assistant", blocks=(call,)),
                CanonicalMessage(
                    role="tool",
                    blocks=(
                        ToolResultBlock(
                            call_id=call.call_id,
                            output="drill-started\ndrill-finished\n",
                            exit_code=0,
                            failed=False,
                        ),
                    ),
                ),
            ),
            round_index=2,
            tools=({"type": "function", "function": {"name": "shell.exec"}},),
        )
    )

    assert second.stop_reason is StopReason.COMPLETED
    assert "drill-finished" in second.text


async def test_the_provider_is_pure_for_the_same_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    first = await provider.complete(_request("complete", 1))
    second = await provider.complete(_request("complete", 1))

    assert first == second


async def test_every_response_reports_one_model_call_and_some_usage() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    for scenario in ("complete", "fail_replay_safe", "continue_once", "shell_once"):
        response = await provider.complete(_request(scenario, 1))
        assert response.model_calls == 1
        assert response.billable_tokens > 0


@pytest.mark.parametrize("delay", [-1, 5001])
def test_the_delay_is_bounded(delay: int) -> None:
    with pytest.raises(ValueError, match="delay"):
        DeterministicModelProvider(delay_ms=delay)


def _long_task(
    round_index: int, text: str = "rounds=3", *, last_failed: bool = False
) -> ModelRequest:
    values = {
        **valid_spec(),
        "model_policy": {"provider": "deterministic", "scenario": "long_task"},
        "tools": ["shell.exec"],
    }
    spec = AgentSpec.model_validate(values)
    messages: tuple[CanonicalMessage, ...] = (
        CanonicalMessage(role="user", blocks=(TextBlock(text=text),)),
    )
    if round_index > 1:
        messages = (
            *messages,
            CanonicalMessage(
                role="tool",
                blocks=(
                    ToolResultBlock(
                        call_id=f"long-task-{round_index - 1}",
                        output="",
                        exit_code=1 if last_failed else 0,
                        failed=last_failed,
                    ),
                ),
            ),
        )
    return ModelRequest(
        policy=spec.model_policy,
        personality=spec.personality,
        messages=messages,
        round_index=round_index,
        tools=({"type": "function", "function": {"name": "shell.exec"}},),
    )


async def test_a_long_task_runs_one_workspace_command_per_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    first = await provider.complete(_long_task(1))
    third = await provider.complete(_long_task(3))

    assert first.stop_reason is StopReason.TOOL_CALL
    assert first.tool_calls[0].name == "shell.exec"
    assert first.tool_calls[0].call_id == "long-task-1"
    # It writes into the Session workspace, so every round commits a revision
    # the way real file-producing work does.
    assert ">> progress.txt" in str(first.tool_calls[0].arguments["command"])
    assert third.tool_calls[0].call_id == "long-task-3"


async def test_a_long_task_without_a_cache_size_holds_nothing_in_its_sandbox() -> None:
    # The tier-A acceptance ran this exact command; its numbers stay comparable.
    response = await DeterministicModelProvider(delay_ms=0).complete(_long_task(1))

    assert response.tool_calls[0].arguments["command"] == "printf 'round 1\\n' >> progress.txt"


async def test_a_long_task_can_hold_memory_in_its_sandbox_cache() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(
        _long_task(2, "rounds=3 cache=64")
    )

    command = str(response.tool_calls[0].arguments["command"])
    # The cache is a tmpfs, so a file there is memory the sandbox holds while
    # frozen. Random bytes, because the VM's swap is zram and zeros would
    # compress to nothing.
    assert "head -c 64M /dev/urandom > /workspace/cache/ballast" in command
    # Written once per sandbox and kept, not once per round.
    assert "[ -f /workspace/cache/ballast ] ||" in command
    assert command.endswith("printf 'round 2\\n' >> progress.txt")


async def test_a_long_task_finishes_after_the_rounds_its_input_asked_for() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(_long_task(4))

    assert response.stop_reason is StopReason.COMPLETED


async def test_a_long_task_counts_rounds_by_the_run_not_by_surviving_results() -> None:
    # Compaction may drop old tool results from the request; counting them
    # would restart the task and it would never end.
    response = await DeterministicModelProvider(delay_ms=0).complete(_long_task(4, "rounds=5"))

    assert response.stop_reason is StopReason.TOOL_CALL
    assert response.tool_calls[0].call_id == "long-task-4"


async def test_a_long_task_fails_when_its_command_failed() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(
        _long_task(2, last_failed=True)
    )

    assert response.stop_reason is StopReason.FAILED


async def test_a_long_task_without_a_round_count_runs_ten() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    tenth = await provider.complete(_long_task(10, "write the report"))
    done = await provider.complete(_long_task(11, "write the report"))

    assert tenth.stop_reason is StopReason.TOOL_CALL
    assert done.stop_reason is StopReason.COMPLETED
